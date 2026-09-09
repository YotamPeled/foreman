# Sandbox runtime (`srt`) probe

Probed 2026-09-08 on this machine. Answer: **yes** — installable and
usable here. The wrapper is **not** rewritten to use it (per the job
spec, this fix is a finding).

## Install

```console
$ npm install -g @anthropic-ai/sandbox-runtime
added 5 packages in 2s
$ srt --version
1.0.0
```

The binary lands at `<node-prefix>/bin/srt` (here
`~/.local/share/mise/installs/node/<version>/bin/srt`), which was
not on a bare login `PATH` — invoked by absolute path below. Source:
`github.com/anthropics/sandbox-runtime` (the `anthropic-experimental`
URL redirects there, HTTP 200).

## Default posture: secure

```console
$ srt -c 'echo srt-ran-ok; cat wt/in.txt; echo new-content > wt/out.txt; cat wt/out.txt'
srt-ran-ok
stdout-marker-42
/usr/bin/bash: line 1: wt/out.txt: Read-only file system
cat: wt/out.txt: No such file or directory
```

Reads work, writes are denied out of the box.

## Trivial worker with a write allowlist

Settings file (`srt-settings.json`):

```json
{
  "network": {"allowedDomains": [], "deniedDomains": ["*"]},
  "filesystem": {"denyRead": [], "allowRead": [],
                 "allowWrite": [".", "/tmp"], "denyWrite": []}
}
```

(`filesystem.denyRead`/`denyWrite` are required keys — omitting them
refuses to run: `Invalid configuration ... Refusing to run with the
default config`.)

```console
$ srt --settings /tmp/srt-probe/settings.json -c 'echo srt-ran-ok; echo new-content > wt/out.txt; cat wt/out.txt; echo "### finished rc=$?"'
srt-ran-ok
new-content
### finished rc=0
```

Exit code 0, the marker reaches stdout, and the file is visible from the
host afterwards. A trivial worker runs through `srt` exactly where
`bash <run.sh>` sits today.

## Caveats before any adoption

- Local bubblewrap is **0.11.2**; the research notes pin `>= 0.12.0`
  for the symlink-traversal write-escape fix. `srt` ran anyway, but the
  box is below the pinned version.
- `srt` is a per-command wrapper (bubblewrap + seccomp + network
  allowlist), not a supervisor: unit lifetime, timeout, and completion
  signalling stay systemd's job.
- Settings are a file on disk the worker's unit would have to carry;
  network defaults to deny-all, so every domain a worker needs (model
  APIs, package registries) must be listed.
