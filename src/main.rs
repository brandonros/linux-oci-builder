use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use futures_util::TryStreamExt;
use oci_client::{Client, Reference, manifest::OciDescriptor, secrets::RegistryAuth};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::io::AsyncWriteExt;

const MEDIA_TYPES: &[&str] = &[
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
];

#[derive(Parser)]
#[command(about = "Pull a public container image into a local OCI image layout")]
struct Args {
    /// Registry reference (the base image from our Dockerfile by default)
    #[arg(default_value = "docker.io/library/alpine:3.22")]
    image: String,
    /// Destination directory; must not already exist
    #[arg(short, long, default_value = "image")]
    output: PathBuf,
    /// OS/architecture[/variant], e.g. linux/amd64 or linux/arm/v7
    #[arg(long, default_value_t = default_platform())]
    platform: String,
}

fn default_platform() -> String {
    let arch = match std::env::consts::ARCH {
        "aarch64" => "arm64",
        "x86_64" => "amd64",
        arch => arch,
    };
    format!("linux/{arch}")
}

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn blob_path(root: &Path, digest: &str) -> Result<PathBuf> {
    let hash = digest
        .strip_prefix("sha256:")
        .context("Only SHA-256 digests are supported")?;
    ensure!(
        hash.len() == 64
            && hash
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()),
        "Invalid SHA-256 digest: {digest}"
    );
    Ok(root.join("blobs/sha256").join(hash))
}

fn select_platform(index: &Value, platform: &str) -> Result<Value> {
    let parts: Vec<_> = platform.split('/').collect();
    ensure!(
        (2..=3).contains(&parts.len()) && parts.iter().all(|p| !p.is_empty()),
        "Platform must be OS/architecture[/variant]"
    );
    let manifests = index["manifests"]
        .as_array()
        .context("Index has no manifests")?;
    let matches: Vec<_> = manifests
        .iter()
        .filter(|m| {
            let p = &m["platform"];
            p["os"].as_str() == Some(parts[0])
                && p["architecture"].as_str() == Some(parts[1])
                && (parts.len() == 2 || p["variant"].as_str() == Some(parts[2]))
        })
        .collect();
    match matches.as_slice() {
        [entry] => Ok((*entry).clone()),
        [] => bail!("No image for platform {platform}"),
        _ => bail!("Multiple images match {platform}; specify a variant"),
    }
}

fn verify(bytes: &[u8], expected: &str, size: Option<u64>) -> Result<()> {
    ensure!(digest(bytes) == expected, "Digest mismatch for {expected}");
    if let Some(size) = size {
        ensure!(bytes.len() as u64 == size, "Size mismatch for {expected}");
    }
    Ok(())
}

async fn download_blob(
    client: &Client,
    image: &Reference,
    root: &Path,
    value: &Value,
) -> Result<()> {
    let descriptor: OciDescriptor = serde_json::from_value(value.clone())?;
    let path = blob_path(root, &descriptor.digest)?;
    let expected_size = value["size"].as_u64().context("Missing blob size")?;
    println!("  {} ({} bytes)", descriptor.digest, expected_size);
    let mut stream = client.pull_blob_stream(image, &descriptor).await?;
    let mut file = tokio::fs::File::create(&path).await?;
    let mut hash = Sha256::new();
    let mut size = 0u64;
    while let Some(chunk) = stream.try_next().await? {
        size += chunk.len() as u64;
        ensure!(size <= expected_size, "Blob exceeds declared size");
        hash.update(&chunk);
        file.write_all(&chunk).await?;
    }
    file.flush().await?;
    ensure!(size == expected_size, "Blob size mismatch");
    ensure!(
        format!("sha256:{:x}", hash.finalize()) == descriptor.digest,
        "Blob digest mismatch"
    );
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let image: Reference = args.image.parse().context("Invalid image reference")?;
    ensure!(
        !args.output.exists(),
        "Output already exists: {}",
        args.output.display()
    );
    let parent = args
        .output
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    std::fs::create_dir_all(parent)?;
    // Publish only a complete layout. Failed downloads disappear with this staging directory.
    let staging = tempfile::tempdir_in(parent)?;
    let root = staging.path();
    tokio::fs::create_dir_all(root.join("blobs/sha256")).await?;
    let client = Client::default();
    let auth = RegistryAuth::Anonymous;
    println!("Resolving {} for {}", args.image, args.platform);
    let (mut raw, initial_digest) = client.pull_manifest_raw(&image, &auth, MEDIA_TYPES).await?;
    verify(&raw, &initial_digest, None)?;
    if let Some(pinned) = image.digest() {
        verify(&raw, pinned, None)?;
    }
    let mut manifest: Value = serde_json::from_slice(&raw)?;
    if manifest.get("manifests").is_some() {
        let selected = select_platform(&manifest, &args.platform)?;
        let selected_digest = selected["digest"]
            .as_str()
            .context("Missing manifest digest")?;
        blob_path(root, selected_digest)?;
        let pinned: Reference = format!(
            "{}/{}@{}",
            image.registry(),
            image.repository(),
            selected_digest
        )
        .parse()?;
        let (bytes, _) = client
            .pull_manifest_raw(&pinned, &auth, MEDIA_TYPES)
            .await?;
        verify(
            &bytes,
            selected_digest,
            Some(selected["size"].as_u64().context("Missing manifest size")?),
        )?;
        raw = bytes;
        manifest = serde_json::from_slice(&raw)?;
    }
    ensure!(
        manifest["schemaVersion"] == 2,
        "Only schema version 2 is supported"
    );
    let media_type = manifest["mediaType"]
        .as_str()
        .context("Missing manifest mediaType")?;
    ensure!(
        media_type == MEDIA_TYPES[1] || media_type == MEDIA_TYPES[3],
        "Expected an image manifest, got {media_type}"
    );
    println!("Downloading config");
    download_blob(&client, &image, root, &manifest["config"]).await?;
    let config_digest = manifest["config"]["digest"]
        .as_str()
        .context("Missing config digest")?;
    let config: Value =
        serde_json::from_slice(&tokio::fs::read(blob_path(root, config_digest)?).await?)?;
    let platform_parts: Vec<_> = args.platform.split('/').collect();
    ensure!(
        (2..=3).contains(&platform_parts.len()) && platform_parts.iter().all(|p| !p.is_empty()),
        "Platform must be OS/architecture[/variant]"
    );
    ensure!(
        config["os"].as_str() == Some(platform_parts[0])
            && config["architecture"].as_str() == Some(platform_parts[1]),
        "Image config does not match requested platform"
    );
    if platform_parts.len() == 3
        && let Some(variant) = config["variant"].as_str()
    {
        ensure!(
            variant == platform_parts[2],
            "Image variant does not match requested platform"
        );
    }
    let layers = manifest["layers"]
        .as_array()
        .context("Missing image layers")?;
    for (i, layer) in layers.iter().enumerate() {
        println!("Downloading layer {}/{}", i + 1, layers.len());
        download_blob(&client, &image, root, layer).await?;
    }
    // Keep original manifest bytes: reformatting JSON changes its digest.
    let manifest_digest = digest(&raw);
    tokio::fs::write(blob_path(root, &manifest_digest)?, &raw).await?;
    let index = json!({
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPES[0],
        "manifests": [{
            "mediaType": media_type,
            "digest": manifest_digest,
            "size": raw.len(),
            "annotations": {"org.opencontainers.image.ref.name": args.image}
        }]
    });
    tokio::fs::write(root.join("index.json"), serde_json::to_vec_pretty(&index)?).await?;
    tokio::fs::write(
        root.join("oci-layout"),
        b"{\"imageLayoutVersion\":\"1.0.0\"}\n",
    )
    .await?;
    ensure!(!args.output.exists(), "Output was created while pulling");
    tokio::fs::rename(root, &args.output).await?;
    println!(
        "Saved {} layers to {}\nManifest: {}",
        layers.len(),
        args.output.display(),
        manifest_digest
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_corruption_and_unsafe_digest_paths() {
        assert!(verify(b"hello", &digest(b"hello"), Some(5)).is_ok());
        assert!(verify(b"changed", &digest(b"hello"), None).is_err());
        assert!(verify(b"hello", &digest(b"hello"), Some(6)).is_err());
        assert!(blob_path(Path::new("."), "sha256:../../etc/passwd").is_err());
    }

    #[test]
    fn selects_platform_and_requires_variant_when_ambiguous() {
        let index = json!({"manifests": [
            {"digest": "a", "platform": {"os": "linux", "architecture": "arm", "variant": "v6"}},
            {"digest": "b", "platform": {"os": "linux", "architecture": "arm", "variant": "v7"}},
            {"digest": "c", "platform": {"os": "unknown", "architecture": "unknown"}}
        ]});
        assert_eq!(
            select_platform(&index, "linux/arm/v7").unwrap()["digest"],
            "b"
        );
        assert!(select_platform(&index, "linux/arm").is_err());
        assert!(select_platform(&index, "linux/amd64").is_err());
        assert!(select_platform(&index, "linux").is_err());
    }
}
