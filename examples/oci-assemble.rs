//! Assemble a derived image; filesystem diff generation belongs to the builder.
use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::Command,
};

const MANIFEST: &str = "application/vnd.oci.image.manifest.v1+json";
const CONFIG: &str = "application/vnd.oci.image.config.v1+json";
const LAYER: &str = "application/vnd.oci.image.layer.v1.tar";

#[derive(Parser)]
#[command(
    about = "Assemble a derived OCI layout from one base image and optional uncompressed tar layer"
)]
struct Args {
    #[arg(long)]
    base: PathBuf,
    #[arg(long)]
    output: PathBuf,
    /// Uncompressed OCI filesystem changeset, including ownership and whiteouts
    #[arg(long)]
    layer: Option<PathBuf>,
    /// JSON object of runtime config fields to replace (arrays are replaced whole)
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long, default_value = "linux-oci-builder OCI assembly")]
    created_by: String,
}

fn blob_path(root: &Path, digest: &str) -> Result<PathBuf> {
    let hash = digest.strip_prefix("sha256:").context("Expected SHA-256")?;
    ensure!(
        hash.len() == 64
            && hash
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "Invalid digest"
    );
    Ok(root.join("blobs/sha256").join(hash))
}

fn store(root: &Path, mut input: impl Read, media: &str) -> Result<Value> {
    let mut temp = tempfile::NamedTempFile::new_in(root.join("blobs/sha256"))?;
    let mut hash = Sha256::new();
    let mut size = 0u64;
    let mut buf = [0; 65536];
    loop {
        let n = input.read(&mut buf)?;
        if n == 0 {
            break;
        }
        temp.write_all(&buf[..n])?;
        hash.update(&buf[..n]);
        size += n as u64;
    }
    let digest = format!("sha256:{:x}", hash.finalize());
    temp.persist(blob_path(root, &digest)?)?;
    Ok(json!({"mediaType":media,"digest":digest,"size":size}))
}

fn import(root: &Path, base: &Path, descriptor: &Value, media: &str) -> Result<Value> {
    let digest = descriptor["digest"].as_str().context("Missing digest")?;
    let result = store(root, fs::File::open(blob_path(base, digest)?)?, media)?;
    ensure!(
        result["digest"] == descriptor["digest"] && result["size"] == descriptor["size"],
        "Base blob digest or size mismatch: {digest}"
    );
    Ok(result)
}

fn read_blob(root: &Path, descriptor: &Value) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(blob_path(
        root,
        descriptor["digest"].as_str().context("Missing digest")?,
    )?)?)?)
}

fn layer_media(media: &str) -> Result<&str> {
    Ok(match media {
        "application/vnd.docker.image.rootfs.diff.tar.gzip" => {
            "application/vnd.oci.image.layer.v1.tar+gzip"
        }
        "application/vnd.docker.image.rootfs.diff.tar" => LAYER,
        LAYER
        | "application/vnd.oci.image.layer.v1.tar+gzip"
        | "application/vnd.oci.image.layer.v1.tar+zstd" => media,
        _ => bail!("Unsupported base layer media type: {media}"),
    })
}

fn validate_tar(path: &Path) -> Result<()> {
    let mut file = fs::File::open(path)?;
    let mut header = [0; 512];
    file.read_exact(&mut header)
        .context("Layer must be an uncompressed tar archive")?;
    ensure!(
        header.iter().all(|b| *b == 0) || &header[257..262] == b"ustar",
        "Expected uncompressed POSIX/GNU tar layer"
    );
    let listing = Command::new("bsdtar")
        .arg("-tf")
        .arg(path)
        .output()
        .context("bsdtar is needed to validate the layer")?;
    ensure!(
        listing.status.success(),
        "Invalid tar layer: {}",
        String::from_utf8_lossy(&listing.stderr)
    );
    Ok(())
}

fn validate_runtime(config: &Value) -> Result<()> {
    if config.is_null() {
        return Ok(());
    }
    ensure!(config.is_object(), "Runtime config must be an object");
    for key in ["User", "WorkingDir", "StopSignal"] {
        ensure!(
            config[key].is_null() || config[key].is_string(),
            "{key} must be a string"
        );
    }
    for key in ["Env", "Entrypoint", "Cmd"] {
        if !config[key].is_null() {
            ensure!(
                config[key]
                    .as_array()
                    .is_some_and(|a| a.iter().all(Value::is_string)),
                "{key} must be an array of strings"
            );
        }
    }
    for key in ["ExposedPorts", "Volumes", "Labels"] {
        if !config[key].is_null() {
            let entries = config[key]
                .as_object()
                .with_context(|| format!("{key} must be an object"))?;
            ensure!(
                entries.values().all(|v| if key == "Labels" {
                    v.is_string()
                } else {
                    v.as_object().is_some_and(|o| o.is_empty())
                }),
                "Invalid {key} value"
            );
        }
    }
    Ok(())
}

fn assemble(args: &Args) -> Result<()> {
    ensure!(!args.output.exists(), "Output already exists");
    ensure!(
        args.layer.is_some() || args.config.is_some(),
        "Supply --layer or --config"
    );
    let layout: Value = serde_json::from_slice(&fs::read(args.base.join("oci-layout"))?)?;
    ensure!(
        layout["imageLayoutVersion"] == "1.0.0",
        "Unsupported layout version"
    );
    let index: Value = serde_json::from_slice(&fs::read(args.base.join("index.json"))?)?;
    let entries = index["manifests"].as_array().context("Missing manifests")?;
    ensure!(
        entries.len() == 1,
        "Select exactly one base image before assembly"
    );
    ensure!(
        matches!(
            entries[0]["mediaType"].as_str(),
            Some(MANIFEST | "application/vnd.docker.distribution.manifest.v2+json")
        ),
        "Base must be an image manifest"
    );
    let parent = args
        .output
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    let staging = tempfile::tempdir_in(parent)?;
    let root = staging.path();
    fs::create_dir_all(root.join("blobs/sha256"))?;
    let old_manifest = import(root, &args.base, &entries[0], MANIFEST)?;
    let base = read_blob(root, &old_manifest)?;
    ensure!(base["schemaVersion"] == 2, "Unsupported manifest schema");
    let old_config = import(root, &args.base, &base["config"], CONFIG)?;
    let mut config = read_blob(root, &old_config)?;
    ensure!(
        config["os"].is_string() && config["architecture"].is_string(),
        "Missing platform"
    );
    ensure!(
        config["rootfs"]["type"] == "layers",
        "Unsupported rootfs type"
    );
    let mut diffs = config["rootfs"]["diff_ids"]
        .as_array()
        .context("Missing diff_ids")?
        .clone();
    let old_layers = base["layers"].as_array().context("Missing layers")?;
    ensure!(
        diffs.len() == old_layers.len(),
        "Layer count differs from diff_ids"
    );
    let mut layers = Vec::new();
    for layer in old_layers {
        layers.push(import(
            root,
            &args.base,
            layer,
            layer_media(layer["mediaType"].as_str().context("Missing media type")?)?,
        )?);
    }
    if let Some(path) = &args.layer {
        // Validate the staged bytes, so the checked archive is the published blob.
        let layer = store(root, fs::File::open(path)?, LAYER)?;
        validate_tar(&blob_path(root, layer["digest"].as_str().unwrap())?)?;
        diffs.push(layer["digest"].clone());
        layers.push(layer);
    }
    if let Some(path) = &args.config {
        let patch: Value = serde_json::from_slice(&fs::read(path)?)?;
        let patch = patch
            .as_object()
            .context("Runtime config patch must be an object")?;
        if config["config"].is_null() {
            config["config"] = json!({});
        }
        config["config"]
            .as_object_mut()
            .context("Invalid base runtime config")?
            .extend(patch.clone());
    }
    config["rootfs"]["diff_ids"] = json!(diffs);
    validate_runtime(&config["config"])?;
    // Do not falsely retain the base image's creation timestamp.
    config
        .as_object_mut()
        .context("Invalid config")?
        .remove("created");
    if config["history"].is_null() {
        config["history"] = json!(vec![json!({}); old_layers.len()]);
    }
    ensure!(
        config["history"]
            .as_array()
            .context("Invalid history")?
            .iter()
            .filter(|h| h["empty_layer"] != true)
            .count()
            == old_layers.len(),
        "Base history does not match its filesystem layers"
    );
    config["history"]
        .as_array_mut()
        .context("Invalid history")?
        .push(json!({"created_by":args.created_by,"empty_layer":args.layer.is_none()}));
    let config_descriptor = store(root, serde_json::to_vec(&config)?.as_slice(), CONFIG)?;
    let manifest =
        json!({"schemaVersion":2,"mediaType":MANIFEST,"config":config_descriptor,"layers":layers});
    let mut descriptor = store(root, serde_json::to_vec(&manifest)?.as_slice(), MANIFEST)?;
    let mut platform = json!({"os":config["os"],"architecture":config["architecture"]});
    for key in ["variant", "os.version", "os.features"] {
        if !config[key].is_null() {
            platform[key] = config[key].clone();
        }
    }
    descriptor["platform"] = platform;
    fs::write(
        root.join("index.json"),
        serde_json::to_vec(
            &json!({"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[descriptor]}),
        )?,
    )?;
    fs::write(
        root.join("oci-layout"),
        b"{\"imageLayoutVersion\":\"1.0.0\"}\n",
    )?;
    fs::rename(root, &args.output)?;
    println!("Assembled {}", args.output.display());
    Ok(())
}

fn main() -> Result<()> {
    assemble(&Args::parse())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(root: &Path) -> Result<()> {
        fs::create_dir_all(root.join("blobs/sha256"))?;
        fs::write(
            root.join("oci-layout"),
            b"{\"imageLayoutVersion\":\"1.0.0\"}",
        )?;
        let config = store(root, serde_json::to_vec(&json!({"architecture":"amd64","os":"linux","config":{"Env":["BASE=kept"],"Cmd":["old"]},"rootfs":{"type":"layers","diff_ids":[]}}))?.as_slice(), CONFIG)?;
        let descriptor = store(
            root,
            serde_json::to_vec(&json!({"schemaVersion":2,"config":config,"layers":[]}))?.as_slice(),
            MANIFEST,
        )?;
        fs::write(
            root.join("index.json"),
            serde_json::to_vec(&json!({"schemaVersion":2,"manifests":[descriptor]}))?,
        )?;
        Ok(())
    }

    #[test]
    fn derives_config_and_layer_without_mutating_base() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let base = dir.path().join("base");
        fixture(&base)?;
        let index_before = fs::read(base.join("index.json"))?;
        let patch = dir.path().join("config.json");
        fs::write(&patch, br#"{"Cmd":["echo","hello"],"User":"1000:1000","WorkingDir":"/work","ExposedPorts":{"8080/tcp":{}}}"#)?;
        let layer = dir.path().join("layer.tar");
        fs::write(&layer, [0; 1024])?;
        let output = dir.path().join("output");
        let args = Args {
            base: base.clone(),
            output: output.clone(),
            layer: Some(layer),
            config: Some(patch),
            created_by: "fixture".into(),
        };
        assemble(&args)?;
        let index: Value = serde_json::from_slice(&fs::read(output.join("index.json"))?)?;
        let manifest = read_blob(&output, &index["manifests"][0])?;
        let config = read_blob(&output, &manifest["config"])?;
        assert_eq!(config["config"]["Env"], json!(["BASE=kept"]));
        assert_eq!(config["config"]["User"], "1000:1000");
        assert_eq!(config["config"]["Cmd"], json!(["echo", "hello"]));
        assert_eq!(
            config["rootfs"]["diff_ids"][0],
            manifest["layers"][0]["digest"]
        );
        assert_eq!(config["history"][0]["empty_layer"], false);
        assert_eq!(fs::read(base.join("index.json"))?, index_before);
        assert!(assemble(&args).is_err());
        Ok(())
    }

    #[test]
    fn config_only_does_not_invent_a_filesystem_layer() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let base = dir.path().join("base");
        fixture(&base)?;
        let patch = dir.path().join("config.json");
        fs::write(&patch, br#"{"Cmd":["echo","hello"]}"#)?;
        let output = dir.path().join("output");
        assemble(&Args {
            base,
            output: output.clone(),
            layer: None,
            config: Some(patch),
            created_by: "CMD".into(),
        })?;
        let index: Value = serde_json::from_slice(&fs::read(output.join("index.json"))?)?;
        let manifest = read_blob(&output, &index["manifests"][0])?;
        let config = read_blob(&output, &manifest["config"])?;
        assert_eq!(manifest["layers"], json!([]));
        assert_eq!(config["rootfs"]["diff_ids"], json!([]));
        assert_eq!(config["history"][0]["empty_layer"], true);
        Ok(())
    }

    #[test]
    fn explicit_null_clears_inherited_command() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let base = dir.path().join("base");
        fixture(&base)?;
        let patch = dir.path().join("config.json");
        fs::write(&patch, br#"{"Cmd":null,"Entrypoint":["server"]}"#)?;
        let output = dir.path().join("output");
        assemble(&Args {
            base, output: output.clone(), layer: None, config: Some(patch),
            created_by: "ENTRYPOINT".into(),
        })?;
        let index: Value = serde_json::from_slice(&fs::read(output.join("index.json"))?)?;
        let manifest = read_blob(&output, &index["manifests"][0])?;
        let config = read_blob(&output, &manifest["config"])?;
        assert!(config["config"]["Cmd"].is_null());
        assert_eq!(config["config"]["Entrypoint"], json!(["server"]));
        Ok(())
    }

    #[test]
    fn rejects_corrupt_base_without_publishing() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let base = dir.path().join("base");
        fixture(&base)?;
        let index: Value = serde_json::from_slice(&fs::read(base.join("index.json"))?)?;
        fs::write(
            blob_path(&base, index["manifests"][0]["digest"].as_str().unwrap())?,
            b"corrupt",
        )?;
        let output = dir.path().join("output");
        let args = Args {
            base,
            output: output.clone(),
            layer: None,
            config: Some(dir.path().join("unused")),
            created_by: "fixture".into(),
        };
        assert!(
            assemble(&args)
                .unwrap_err()
                .to_string()
                .contains("mismatch")
        );
        assert!(!output.exists());
        Ok(())
    }
}
