#!/usr/bin/env python3
"""Build an immutable diagnostic runtime; temporary SSE4/maps patches are explicit.

This does not turn the placeholder /proc/self/maps fields into Linux semantics.
Use for investigating the target builds while production maps support is pending.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("ext/blink"))
    parser.add_argument("--make", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    output = args.output.resolve()
    if output.exists():
        raise ValueError("Output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = repo / "blink/procfs.c"
    cpu = repo / "blink/cpuid.c"
    proc_original, cpu_original = proc.read_text(), cpu.read_text()
    assert "PROCFS_PIDDIR_MAPS_TYPE" not in proc_original
    for token in ("cx |= 0 << 19;   // sse4.1", "cx |= 0 << 20;   // sse4.2"):
        assert token in cpu_original
    source = proc_original.replace('#include "blink/machine.h"', '#include "blink/machine.h"\n#include "blink/pml4t.h"')
    source = source.replace('  PROCFS_PIDDIR_FDDIR_TYPE,', '  PROCFS_PIDDIR_MAPS_TYPE,\n  PROCFS_PIDDIR_FDDIR_TYPE,', 1)
    source = source.replace('static int ProcfsPiddirReaddir(', 'static int ProcfsPiddirMapsRead(struct VfsInfo *, struct ProcfsOpenFile *);\nstatic int ProcfsPiddirReaddir(', 1)
    source = source.replace('    [PROCFS_PIDDIR_FDDIR_TYPE - PROCFS_PIDDIR_TYPE]',
        '    [PROCFS_PIDDIR_MAPS_TYPE - PROCFS_PIDDIR_TYPE] = {0, S_IFREG | 0444, 0, 0, PROCFS_PIDDIR_MAPS_TYPE, "maps", .read = ProcfsPiddirMapsRead},\n    [PROCFS_PIDDIR_FDDIR_TYPE - PROCFS_PIDDIR_TYPE]', 1)
    source += '''
#ifndef DISABLE_VFS
/* DIAGNOSTIC ONLY: addresses real, permissions/backing fields placeholders. */
static int ProcfsPiddirMapsRead(struct VfsInfo *info, struct ProcfsOpenFile *f) {
  struct ContiguousMemoryRanges ranges = {0};
  LOCK(&g_machine->system->mmap_lock);
  FindContiguousMemoryRanges(g_machine, &ranges);
  UNLOCK(&g_machine->system->mmap_lock);
  f->readbufstart = 0;
  if (f->index < ranges.i) {
    struct ContiguousMemoryRange *r = &ranges.p[f->index++];
    f->readbufend = snprintf(f->readbuf, sizeof(f->readbuf),
        "%012llx-%012llx rw-p 00000000 00:00 0\\n",
        (unsigned long long)r->a, (unsigned long long)r->b);
  } else {
    f->readbufstart = f->readbufend = sizeof(f->readbuf) + 1;
  }
  free(ranges.p);
  return 0;
}
#endif
'''
    try:
        proc.write_text(source)
        cpu.write_text(cpu_original.replace('cx |= 0 << 19;   // sse4.1', 'cx |= 1 << 19;   // diagnostic SSE4.1')
                       .replace('cx |= 0 << 20;   // sse4.2', 'cx |= 1 << 20;   // diagnostic SSE4.2'))
        with output.with_suffix(".build.log").open("w") as log:
            subprocess.run([args.make, "-j4", "VM=o/opt/blink/blink -j", "o//blink/blink"],
                           cwd=repo, stdout=log, stderr=log, check=True)
        shutil.copy2(repo / "o/blink/blink", output)
        metadata = {"sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    "diagnostics": ["SSE4 advertisement", "placeholder maps permissions/backing fields"],
                    "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
                    "sources": {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted((repo / "blink").glob("*")) if p.is_file() and p.suffix in (".c", ".h")},
                    "config": (repo / "config.mk").read_text()}
        output.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    finally:
        proc.write_text(proc_original)
        cpu.write_text(cpu_original)
    print(output)


if __name__ == "__main__":
    main()
