use anyhow::{Context, Result, ensure};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};

fn blob(image: &std::path::Path, descriptor: &Value) -> Result<PathBuf> {
    let digest = descriptor["digest"].as_str().context("Missing digest")?;
    let hash = digest.strip_prefix("sha256:").context("Expected SHA-256")?;
    ensure!(
        hash.len() == 64 && hash.bytes().all(|c| c.is_ascii_hexdigit()),
        "Invalid digest"
    );
    let path = image.join("blobs/sha256").join(hash);
    let bytes = fs::read(&path)?;
    ensure!(
        format!("{:x}", Sha256::digest(&bytes)) == hash,
        "Digest mismatch"
    );
    ensure!(
        Some(bytes.len() as u64) == descriptor["size"].as_u64(),
        "Size mismatch"
    );
    Ok(path)
}

pub fn prepare(image: &Path) -> Result<(tempfile::TempDir, PathBuf)> {
    let index: Value = serde_json::from_slice(&fs::read(image.join("index.json"))?)?;
    let manifest: Value = serde_json::from_slice(&fs::read(blob(image, &index["manifests"][0])?)?)?;
    let config: Value = serde_json::from_slice(&fs::read(blob(image, &manifest["config"])?)?)?;
    ensure!(
        config["os"] == "linux" && config["architecture"] == "amd64",
        "Blink needs a linux/amd64 image"
    );
    let layers = manifest["layers"].as_array().context("Missing layers")?;
    let mut archives = Vec::new();
    for descriptor in layers {
        let layer = blob(image, descriptor)?;
        let listing = Command::new("bsdtar").arg("-tf").arg(&layer).output()?;
        ensure!(listing.status.success(), "Could not list layer archive");
        ensure!(
            !String::from_utf8_lossy(&listing.stdout)
                .lines()
                .any(|name| name.split('/').any(|part| part.starts_with(".wh."))),
            "Whiteouts need a real OCI layer unpacker"
        );
        archives.push(layer);
    }

    fs::create_dir_all("target/blink-experiment")?;
    let work = tempfile::Builder::new()
        .prefix("run-")
        .tempdir_in("target/blink-experiment")?;
    let root = work.path().join("rootfs");
    fs::create_dir(&root)?;
    // bsdtar preserves Linux absolute symlinks without following them during extraction.
    // Ownership remains that of the host user: this is not an image layer exporter.
    // Apply layers in manifest order so later entries replace earlier files.
    for layer in archives {
        let status = Command::new("bsdtar")
            .arg("-xf")
            .arg(layer)
            .arg("-C")
            .arg(&root)
            .arg("--no-same-owner")
            .status()?;
        ensure!(status.success(), "Layer extraction failed");
    }
    Ok((work, root))
}
