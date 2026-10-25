#!/usr/bin/env python3
"""Verify target OCI contents and metadata without executing the image."""
import argparse
import hashlib
import importlib.util
import json
import posixpath
from pathlib import Path, PurePosixPath
import tarfile

spec = importlib.util.spec_from_file_location("run_image", Path(__file__).with_name("run-image.py"))
oci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oci)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--kind", choices=("claude-base", "code-server"), required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest, config, layers = oci.verify(args.image.resolve())
    entries, errors = {}, []

    def check(condition, message):
        if not condition:
            errors.append(message)

    for layer in layers:
        with tarfile.open(layer) as archive:
            members = archive.getmembers()
            # OCI whiteouts affect lower layers, not sibling entries in this layer.
            for member in members:
                name = str(PurePosixPath(member.name))
                path = PurePosixPath(name)
                if path.name == ".wh..wh..opq":
                    prefix = "" if str(path.parent) == "." else str(path.parent)
                    entries = {k: v for k, v in entries.items()
                               if not (k.startswith(prefix + "/") if prefix else k != ".")}
                elif path.name.startswith(".wh."):
                    target = str(path.parent / path.name[4:])
                    entries = {k: v for k, v in entries.items()
                               if k != target and not k.startswith(target + "/")}
            for member in members:
                name = str(PurePosixPath(member.name))
                if PurePosixPath(name).name.startswith(".wh."):
                    continue
                row = {"uid": member.uid, "gid": member.gid, "mode": member.mode,
                       "size": member.size, "link": member.linkname, "type": member.type.decode("ascii")}
                if member.isfile():
                    with archive.extractfile(member) as data:
                        content = data.read() if member.size <= 1024 * 1024 else data.read(4)
                    row["magic"] = content[:4].hex()
                    if member.size <= 1024 * 1024:
                        row["sha256"] = hashlib.sha256(content).hexdigest()
                        if name.endswith(".json") or name == "etc/passwd":
                            row["text"] = content.decode("utf-8", errors="replace")
                elif member.islnk() and member.linkname in entries:
                    for key in ("magic", "sha256", "text", "size"):
                        if key in entries[member.linkname]:
                            row[key] = entries[member.linkname][key]
                entries[name] = row

    runtime = config["config"]
    check(runtime.get("User") == "coder", "Config.User must be coder")
    check(runtime.get("WorkingDir") == "/home/coder/workspace", "Incorrect working directory")
    check(any(x.startswith("PATH=/home/coder/.local/bin:") for x in runtime.get("Env", [])), "Missing Claude PATH")
    check(any(line.split(":")[0:4] == ["coder", "x", "1000", "1000"]
              for line in entries.get("etc/passwd", {}).get("text", "").splitlines()), "Missing coder UID/GID 1000")
    bad_owners = [name for name, row in entries.items()
                  if (name == "home/coder" or name.startswith("home/coder/"))
                  and (row["uid"], row["gid"]) != (1000, 1000)]
    check(not bad_owners, f"Incorrect coder ownership on {len(bad_owners)} entries: {bad_owners[:10]}")
    binaries = [name for name, row in entries.items()
                if name.startswith("home/coder/.local/share/claude/versions/")
                and row.get("magic") == "7f454c46" and row["size"] > 10 * 1024 * 1024]
    check(bool(binaries), "No installed Claude ELF executable")
    check("home/coder/.local/bin/claude" in entries, "Missing Claude command link")
    command = "home/coder/.local/bin/claude"
    for _ in range(16):
        row = entries.get(command, {})
        if row.get("type") != "2":
            break
        command = posixpath.normpath(posixpath.join(posixpath.dirname(command), row["link"])).lstrip("/")
    check(command in binaries, "Claude command does not resolve to the installed executable")
    check(entries.get("usr/bin/passwd", {}).get("mode") == 0o4755, "passwd setuid mode was not preserved")
    check(entries.get("usr/bin/chage", {}).get("mode") == 0o2755, "chage setgid mode was not preserved")

    copies = {"apps/claude-base/managed-settings.json": "etc/claude-code/managed-settings.json",
              "apps/claude-base/CLAUDE.md": "etc/claude-code/CLAUDE.md"}
    for source, target in [("apps/claude-base/org", "opt/claude-org"),
                           ("agents", "opt/claude-org/plugins/org/agents"),
                           ("skills", "opt/claude-org/plugins/org/skills")]:
        directory = args.context / source
        for file in directory.rglob("*"):
            if file.is_file():
                copies[str(file.relative_to(args.context))] = str(PurePosixPath(target) / file.relative_to(directory))
    for source, target in copies.items():
        row = entries.get(target, {})
        check(row.get("sha256") == hashlib.sha256((args.context / source).read_bytes()).hexdigest(), "COPY content mismatch: " + target)
        check((row.get("uid"), row.get("gid")) == (0, 0), "COPY ownership mismatch: " + target)

    # Debian's Perl package contains distinct case-sensitive Linux paths.
    for directory in ("usr/lib/x86_64-linux-gnu/perl/5.40.1/sys",
                      "usr/lib/x86_64-linux-gnu/perl/5.40.1/Sys",
                      "usr/share/perl/5.40.1/pod", "usr/share/perl/5.40.1/Pod"):
        check(entries.get(directory, {}).get("type") == "5", "Missing distinct Linux directory: " + directory)
    check("usr/lib/x86_64-linux-gnu/perl/5.40.1/sys/ioctl.ph" in entries, "Perl sys/ioctl.ph lost or renamed")
    check("usr/share/perl/5.40.1/pod/perldiag.pod" in entries, "Perl pod/perldiag.pod lost or renamed")
    base_recipe = (args.context / "apps/claude-base/Dockerfile").read_text()
    if "gh auth git-credential" in base_recipe:
        check(entries.get("usr/bin/gh", {}).get("magic") == "7f454c46", "Missing GitHub CLI executable")
        check("etc/gitconfig" in entries, "Missing system Git configuration")

    if args.kind == "code-server":
        package = json.loads(entries.get("usr/lib/code-server/package.json", {}).get("text", "{}"))
        check(package.get("version") == "4.141.0", "Incorrect code-server package version")
        check(runtime.get("Entrypoint") == ["dumb-init", "code-server"], "Incorrect code-server entrypoint")
        check(not runtime.get("Cmd"), "Inherited CMD was not cleared by ENTRYPOINT")
        check("8080/tcp" in runtime.get("ExposedPorts", {}), "Missing port 8080 metadata")
        check(not any(name.startswith("usr/lib/code-server/lib/vscode/extensions/copilot/") for name in entries), "Bundled Copilot remains")
        recipe = (args.context / "apps/code-server/Dockerfile").read_text()
        location = "Machine" if "/Machine/settings.json" in recipe else "User"
        settings = json.loads(entries.get(f"home/coder/.local/share/code-server/{location}/settings.json", {}).get("text", "{}"))
        if "serve-web-key-half" in recipe:
            key = entries.get("home/coder/.local/share/code-server/serve-web-key-half", {})
            check(key.get("type") == "2" and key.get("link") == "/home/coder/.local/share/code-server/User/serve-web-key-half", "Missing persistent sign-in key symlink")
        check(settings == {"chat.disableAIFeatures": True, "claudeCode.preferredLocation": "panel", "workbench.startupEditor": "none"}, "Incorrect editor settings")
        installed = []
        for name, row in entries.items():
            if name.startswith("home/coder/.local/share/code-server/extensions/") and name.count("/") == 7 and name.endswith("/package.json"):
                package = json.loads(row.get("text", "{}"))
                installed.append((str(package.get("publisher")) + "." + str(package.get("name"))).lower())
        check("anthropic.claude-code" in installed, "Claude extension missing")
        check("org.command-center" in installed, "Command Center extension missing")
        if "GitHub.vscode-pull-request-github" in recipe:
            check("github.vscode-pull-request-github" in installed, "GitHub pull-request extension missing")
    report = {"manifest": digest, "kind": args.kind, "entries": len(entries),
              "claude_binaries": binaries, "copied_files_checked": len(copies), "errors": errors}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
