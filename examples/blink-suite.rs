//! Executes a deliberately restricted Dockerfile subset against a fresh rootfs.
use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use serde_json::json;
use std::{
    fs,
    os::unix::process::CommandExt,
    path::PathBuf,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
mod support;

#[derive(Parser)]
struct Args {
    #[arg(long, default_value = "target/blink-experiment/bin/blink")]
    blink: PathBuf,
    #[arg(long, default_value = "target/blink-experiment/image")]
    image: PathBuf,
    /// Directory containing the Dockerfile recipes to test
    #[arg(long, default_value = "examples/dockerfiles")]
    recipes: PathBuf,
    /// Disable Blink's JIT to compare interpreter behavior
    #[arg(long)]
    no_jit: bool,
    /// Diagnostic workaround: tell glibc to use TCP DNS (does not fix UDP support)
    #[arg(long)]
    dns_tcp: bool,
    /// JSON report for this invocation
    #[arg(long, default_value = "target/blink-experiment/suite-results.json")]
    report: PathBuf,
    /// Include the package-installation experiment and supply host DNS configuration
    #[arg(long)]
    network: bool,
    /// Run just recipes whose filename contains this string
    #[arg(long)]
    filter: Option<String>,
    #[arg(long, default_value_t = 30, value_parser = clap::value_parser!(u64).range(1..))]
    timeout: u64,
}

#[derive(Debug)]
struct Step {
    label: String,
    argv: Vec<String>,
}

fn parse(source: &str, expected_image: &str) -> Result<Vec<Step>> {
    let mut steps = Vec::new();
    let mut from = false;
    let mut cmd = None;
    for (line_no, line) in source.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (instruction, value) = line
            .split_once(' ')
            .context("Expected instruction and arguments")?;
        let value = value.trim();
        ensure!(
            !value.ends_with('\\'),
            "Line continuations are not supported"
        );
        if !from {
            ensure!(instruction == "FROM", "First instruction must be FROM");
            let reference: oci_client::Reference = value
                .parse()
                .context("Expected a single image reference in FROM")?;
            let expected: oci_client::Reference = expected_image.parse()?;
            ensure!(
                reference.to_string() == expected.to_string(),
                "FROM {value} does not match the pulled image {expected_image}"
            );
            from = true;
            continue;
        }
        match instruction {
            "RUN" => {
                ensure!(
                    !value.starts_with('[') && !value.starts_with("--"),
                    "Only shell-form RUN without flags is supported"
                );
                steps.push(Step {
                    label: format!("line {} RUN", line_no + 1),
                    argv: vec!["/bin/sh".into(), "-c".into(), format!("cd / && {value}")],
                });
            }
            "CMD" => {
                let argv: Vec<String> =
                    serde_json::from_str(value).context("CMD requires a JSON string array")?;
                ensure!(
                    !argv.is_empty() && argv[0].starts_with('/'),
                    "CMD needs an absolute executable path"
                );
                cmd = Some(Step {
                    label: "CMD (runtime smoke check)".into(),
                    argv,
                });
            }
            _ => bail!("Unsupported instruction {instruction}; this is an execution experiment"),
        }
    }
    ensure!(from, "Missing FROM");
    if let Some(cmd) = cmd {
        steps.push(cmd);
    }
    Ok(steps)
}

fn run_case(args: &Args, recipe: &std::path::Path) -> Result<PathBuf> {
    let index: serde_json::Value =
        serde_json::from_slice(&fs::read(args.image.join("index.json"))?)?;
    let reference = index["manifests"][0]["annotations"]["org.opencontainers.image.ref.name"]
        .as_str()
        .context("Image layout needs its source reference annotation")?;
    let steps = parse(&fs::read_to_string(recipe)?, reference)?;
    let (temp, root) = support::prepare(&args.image)?;
    let saved = temp.keep();
    println!("  artifacts: {}", saved.display());
    if args.network {
        fs::copy("/etc/resolv.conf", root.join("etc/resolv.conf"))?;
    }
    for (i, step) in steps.iter().enumerate() {
        println!("  {}", step.label);
        let stdout = saved.join(format!("{i}.stdout"));
        let stderr = saved.join(format!("{i}.stderr"));
        let mut child = Command::new(&args.blink)
            .env_clear()
            .env(
                "PATH",
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            )
            .env("HOME", "/root")
            .envs(args.dns_tcp.then_some(("RES_OPTIONS", "use-vc")))
            .arg("-m")
            .args(args.no_jit.then_some("-j"))
            .arg("-C")
            .arg(root.canonicalize()?)
            .arg("-L")
            .arg(saved.join(format!("{i}.blink.log")))
            // Enter the guest root for exec-form CMD as well as shell-form RUN.
            .args(["/bin/sh", "-c", "cd / && exec \"$@\"", "image-forge"])
            .args(&step.argv)
            .stdin(Stdio::null())
            .stdout(fs::File::create(&stdout)?)
            .stderr(fs::File::create(&stderr)?)
            .process_group(0)
            .spawn()?;
        let start = Instant::now();
        let status = loop {
            if let Some(status) = child.try_wait()? {
                break status;
            }
            if start.elapsed() >= Duration::from_secs(args.timeout) {
                // Kill the process group so forked guest children do not outlive the test.
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                child.wait()?;
                bail!(
                    "{} timed out after {}s; logs: {}",
                    step.label,
                    args.timeout,
                    saved.display()
                );
            }
            thread::sleep(Duration::from_millis(50));
        };
        let out = fs::read_to_string(stdout)?;
        let err = fs::read_to_string(stderr)?;
        if !out.is_empty() {
            print!("{out}");
        }
        if !err.is_empty() {
            eprint!("{err}");
        }
        ensure!(
            status.success(),
            "{} failed ({status}); logs: {}",
            step.label,
            saved.display()
        );
    }
    Ok(saved)
}

fn main() -> Result<()> {
    let mut args = Args::parse();
    args.blink = args
        .blink
        .canonicalize()
        .context("Build Blink first; see README")?;
    let mut recipes = fs::read_dir(&args.recipes)?
        .map(|e| e.map(|e| e.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    recipes.sort();
    let mut results = Vec::new();
    let mut failed = 0;
    for recipe in recipes {
        let name = recipe.file_name().unwrap().to_string_lossy().to_string();
        if recipe.extension().and_then(|e| e.to_str()) != Some("Dockerfile") {
            continue;
        }
        if args.filter.as_ref().is_some_and(|f| !name.contains(f)) {
            continue;
        }
        if (name == "06-apk.Dockerfile"
            || fs::read_to_string(&recipe)?
                .lines()
                .any(|line| line == "# image-forge: network"))
            && !args.network
        {
            println!("SKIP {name} (use --network)");
            results.push(json!({"recipe": name, "status": "skipped"}));
            continue;
        }
        println!("TRY {name}");
        match run_case(&args, &recipe) {
            Ok(path) => {
                println!("PASS {name}");
                results.push(json!({"recipe": name, "status": "passed", "artifacts": path}));
            }
            Err(error) => {
                failed += 1;
                println!("FAIL {name}: {error:#}");
                results.push(
                    json!({"recipe": name, "status": "failed", "error": format!("{error:#}")}),
                );
            }
        }
    }
    ensure!(!results.is_empty(), "No recipes matched");
    fs::create_dir_all("target/blink-experiment")?;
    fs::write(&args.report, serde_json::to_vec_pretty(&results)?)?;
    println!("Results: {}", args.report.display());
    ensure!(failed == 0, "{failed} experiment(s) failed");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn parse(source: &str) -> Result<Vec<Step>> {
        super::parse(source, "docker.io/library/alpine:3.22")
    }
    #[test]
    fn accepts_matching_debian_and_rejects_wrong_base() {
        let source = "FROM debian:trixie-slim\nRUN true";
        assert!(super::parse(source, "docker.io/library/debian:trixie-slim").is_ok());
        assert!(parse(source).is_err());
    }
    #[test]
    fn rejects_unsupported_recipes() {
        assert!(parse("FROM ubuntu:latest\nRUN true").is_err());
        assert!(parse("FROM docker.io/library/alpine:3.22\nCOPY a b").is_err());
        assert!(parse("FROM docker.io/library/alpine:3.22\nRUN --mount=type=cache true").is_err());
    }
    #[test]
    fn runs_only_last_cmd_after_build_steps() {
        let steps = parse("FROM docker.io/library/alpine:3.22\nCMD [\"/bin/false\"]\nRUN echo hi\nCMD [\"/bin/true\"]").unwrap();
        assert_eq!(steps.len(), 2);
        assert_eq!(steps[1].argv, vec!["/bin/true"]);
    }
}
