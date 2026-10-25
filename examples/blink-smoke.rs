//! A deliberately narrow execution experiment, not a Dockerfile builder.
use std::{fs, path::PathBuf, process::Command};

use anyhow::{Context, Result, ensure};
use clap::Parser;
mod support;

#[derive(Parser)]
struct Args {
    #[arg(long, default_value = "target/blink-experiment/bin/blink")]
    blink: PathBuf,
    #[arg(long, default_value = "target/blink-experiment/image")]
    image: PathBuf,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let blink = args
        .blink
        .canonicalize()
        .context("Build Blink first; see README")?;
    let (work, root) = support::prepare(&args.image)?;
    let log = work.path().join("blink.log");
    let output = Command::new(&blink)
        .env_clear().env("PATH", "/usr/bin:/bin:/sbin")
        .arg("-m").arg("-C").arg(root.canonicalize()?)
        .arg("-L").arg(&log)
        .args(["/bin/sh", "-c", "cd / && echo 'Hello from Linux on macOS' > /hello.txt && /bin/cat /hello.txt && /bin/uname -m && /bin/sh -c 'echo child-process-ok' && /sbin/apk --version"])
        .output()?;
    print!("{}", String::from_utf8_lossy(&output.stdout));
    eprint!("{}", String::from_utf8_lossy(&output.stderr));
    let saved = work.keep();
    ensure!(
        output.status.success(),
        "Blink failed: {}; artifacts: {}",
        output.status,
        saved.display()
    );
    ensure!(
        fs::read_to_string(root.join("hello.txt"))? == "Hello from Linux on macOS\n",
        "Guest file write did not reach rootfs"
    );
    ensure!(
        String::from_utf8_lossy(&output.stdout).contains("child-process-ok"),
        "Child process did not run"
    );
    println!(
        "Verified Linux execution and filesystem write. Artifacts: {}",
        saved.display()
    );
    Ok(())
}
