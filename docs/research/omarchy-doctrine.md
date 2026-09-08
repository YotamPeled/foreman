# Omarchy doctrine, mapped onto Foreman

Studied against the installed package at `/usr/share/omarchy` (428 scripts in `bin/`, 96 migrations, `shell/` = one Quickshell process). Every claim is CONFIRMED from a file read unless marked UNVERIFIED. Companion: `omarchy-shell-surfaces.md` (plugin kinds, IPC, theme tokens).

## 1. Layering

Four layers; the ordering rule is **load order, not merge**.

- `$OMARCHY_PATH/default/` — packaged code, executed in place, never copied. `$OMARCHY_PATH/config/` — *templates* for `~/.config`, copied on install and refresh. `~/.config/` — the user's, authoritative. `~/.local/state/omarchy/` — generated, and on the load path.
- `default/hypr/bootstrap.lua` sets `package.path` to `~/.local/state/?.lua` → `~/.config/?.lua` → `$OMARCHY_PATH/?.lua`. First hit wins: state shadows user shadows package.
- `config/hypr/hyprland.lua` (the copy the user owns) calls `require("default.hypr.omarchy")` **first**, then `hypr.monitors/input/bindings/looknfeel/autostart`. Defaults improve under the user because the user's calls run last. Opting out is a global, not a fork: `omarchy_default_bindings = false` is read in `default/hypr/omarchy.lua`.
- Overlay: `default/hypr/require_optional.lua` is a two-line `package.searchpath` guard, so an absent overlay is a no-op, not an error. The current theme is itself an optional module (`omarchy.current.theme.hyprland`).
- **No deep merge where it would be ambiguous.** `shell.qml:73-87`: a user `shell.json` with `version: 1` replaces the packaged `config/omarchy/shell.json` entirely; a bad parse or wrong version falls back to the packaged file, and that to a `builtinShellConfig` literal in QML. Three-deep ladder, zero merging.
- `omarchy refresh config <p>` copies `config/<p>` → `~/.config/<p>`, saves `<file>.bak.<epoch>`, deletes the backup if identical, prints a `diff` if not. `refresh hyprland` is six calls to it. Per-file, opt-in, destructive-with-receipt.
- Clone, never edit the packaged copy: `omarchy plugin clone omarchy.clock` copies the built-in directory to `~/.config/omarchy/plugins/<username>.clock`, rewrites the id, keeps the widget's position and settings, and **routes IPC addressed to the built-in id to the clone**, so callers don't change. Removing it falls back to the built-in.
- Themes overlay the same way: `omarchy-theme-set` copies `themes/<n>/*` into a staging dir, overlays `~/.config/omarchy/themes/<n>/*` on top, then `mv`s staging onto `~/.local/state/omarchy/current/theme`.
- `etc-overrides/` holds 7 files for `/etc`; only `cups-cups-files.conf` has a confirmed installer (`install/post-install/pacman.sh`) — who applies the other six is UNVERIFIED.

## 2. Mutation surface

Every mutation is a script named `omarchy-<group>-<verb>`; there is no generic "set" API.

| Command | Writes | How the running system notices |
|---|---|---|
| `theme set <n>` | staging dir → atomic `mv` onto `state/current/theme`; `current/theme.name`; `current/background` symlink; `flock` on a runtime lock | pushes base64 `colors.toml`/`shell.toml` via `omarchy-shell shell applyTheme`, then 15 per-app retint scripts in parallel, then `omarchy-hook theme-set` |
| `bar move/put/set/use/position` | `~/.config/omarchy/shell.json` | shell `FileView`, `onFileChanged: reload()`; `shell reloadConfig` forces it |
| `plugin add/clone/enable/disable/remove` | checkout in `~/.config/omarchy/plugins/<id>/`; enabled-ness = **presence in shell.json** (3p: id appears; 1p: absent from `disabledPlugins[]`) | `PluginRegistry` runs `inotifywait -m -r -e close_write,create,delete,move` on the plugins dir and hot-reloads; `rescanPlugins` forces a re-walk |
| `hyprland toggle <flag> on\|off` | copies `default/hypr/toggles/<flag>.lua` into `state/toggles/hypr/`, a dir sourced wholesale | `hyprctl reload` |
| `toggle enabled <flag>` | nothing — it *is* `[[ -f state/toggles/<flag> ]]`; file presence is the state | — |
| `restart shell` | nothing | kills every `quickshell` on that config path, relaunches via `hyprctl dispatch exec` so it inherits the session env, polls `shell ping` 20×100ms, re-locks if the session was locked |
| `reminder <min> [msg]` | transient `systemd --user` timers, read back as JSON from `systemctl list-timers` | `omarchy-shell -q omarchy.indicators refresh` |

`omarchy-shell [-q] <target> <method> [args]` wraps `qs ipc -n -p $OMARCHY_PATH/shell call`. It never starts the shell, and it distinguishes not-running, not-responding (`timeout` 124/137) and "not ready to accept queries yet" as separate failures; `-q` is best-effort-and-exit-0, for callers that must not fail.

**Config vs state.** `~/.config/omarchy/` = what the user chose (`shell.json`, `plugins/`, `themes/`, `hooks/`, `extensions/`, `branding/`). `~/.local/state/omarchy/` = what the runtime derived (`current/theme` and the `current/background` symlink, `migrations/` markers, `toggles/`, `settings/`, `notifications/`, `indicators/`). Nothing in state is hand-edited; deleting it is recoverable.

## 3. Extensibility

- **Plugin manifest** (`PluginRegistry.qml:48-88`, mirrored by `omarchy-plugin-validate`): required `schemaVersion: 1, id, name, version, kinds[], entryPoints{}`; entry points must be safe relative paths that exist and are not symlinks, and the id must not be reserved. Optional `author, description, keepLoaded, barWidget{displayName, category, allowMultiple, defaultSection, defaults, schema[]}, omarchy.clonePaths[]`. Six kinds; one `bar` active at a time, an invalid choice falls back to the built-in.
- The installer "never runs plugin code, install hooks, or sudo — it only clones files, validates the manifest, and toggles enabled state over shell IPC" (`shell/README.md`). Plugins land **disabled** for review, updates show a diff, and `--yes` is the documented path for scripts and AI agents.
- **Menu extension**: `~/.config/omarchy/extensions/omarchy-menu.jsonc`, watched, layered over `default/omarchy/omarchy-menu.jsonc`. Dotted keys define the tree; reusing an id overrides it field-by-field. Fields `icon, label, action, target, provider, aliases, description, when` (hide condition), `checked` (✓ condition). `provider` makes a row dynamic from a command returning JSON rows.
- **Hooks**: `omarchy-hook <name> [args]` runs `~/.config/omarchy/hooks/<name>` then every file in `<name>.d/` (skipping `*.sample`); a failure prints and continues. Shipped: `theme-set, font-set, battery-low, post-update, pre-refresh-pacman, post-boot` — call sites confirmed for the first five, `post-boot`'s is UNVERIFIED. `omarchy hook install <type> <file>` copies and chmods 755.
- **Themes as directories**: `colors.toml` canonical, plus optional per-app files, `backgrounds/`, previews. A theme installed *from a git repo* (detected by a `.git` dir) is held to colour only — any `.lua`, terminal config or `vscode.json` is dropped and reported, because those name programs to launch or extensions to install. A hand-written dir, or a symlink to your own checkout, is trusted.
- Adding a **command** touches no registry: drop an executable named `omarchy-<group>-<verb>` in `bin/`.

## 4. Migrations

- `migrations/<unix-timestamp>.sh`, timestamp = the authoring commit's date (`omarchy-dev-add-migration`: `git log -1 --format=%cd --date=unix`). Lexical order = chronological order.
- `omarchy migrate`: pending iff no marker of that name in `~/.local/state/omarchy/migrations/`; runs under `bash -euo pipefail`, then touches the marker. `--pending` exits 0/1, usable as a shell condition. Entries are fed on **fd 3** and each migration runs with `3<&-`, so one that reads stdin cannot swallow the queue.
- It waits up to 900s on `/var/lib/pacman/db.lck`, and if still held **exits 0 and retries next login** — a migration run never blocks the machine.
- Idempotence is by guard, not by design: each opens with a condition that makes a re-run a no-op (marker present, config already written, `grep` for the exact shipped pairing before rewriting it).
- Failure split, worth copying: a condition it *cannot repair* prints a notice and `exit 0` so it doesn't hold up everything queued behind it; only missing privileges exits nonzero and stays pending, because re-running from a terminal fixes that.
- No schema integer — the marker set *is* the version. Data files carry their own (`shell.json`'s `version: 1`, with a fallback to defaults on an unknown one rather than a guess).

## 5. Discoverability

`bin/omarchy` builds the command tree at runtime by scanning the **first 80 lines** of every file in `bin/` for `# omarchy:<key>=<value>` — keys `summary, group, name, args, examples, alias(es), requires-sudo, hidden`. Missing values are derived from the filename (`omarchy-<group>-<rest>` → route `omarchy <group> <rest>`), and the first plain comment line is the fallback summary; route collisions are detected and reported. `omarchy --help` lists groups against a hardcoded description map, `omarchy <group> --help` lists a group, `omarchy commands [--all] [--json]` dumps the tree machine-readably. `--help` is searched across the whole remaining argv so it can never be forwarded into the real command by accident. `omarchy-menu` is the same surface for the mouse; the prose manual is external (`omarchy.org/manual/`), reached from the menu's `learn` submenu.

## 6. Doctor / health

There is **no `omarchy doctor`**. Health is a set of narrow verbs with exit codes plus one human dump: `omarchy commands --check` (validates CLI metadata for all 427 commands — a self-check of the extension convention), `omarchy plugin validate <folder>` (deliberately mirrors the registry's checks "so the CLI refuses to install anything the running shell would silently reject"), `omarchy migrate --pending`, `omarchy update available`, `omarchy update analyze logs`, `omarchy cmd present|missing`, and `omarchy debug [--print]` (inxi, dmesg, `journalctl -b -p 4..1`, package list → upload/save/view).

---

## Mapping

### Layering

| Omarchy | Foreman |
|---|---|
| `default/` executed in place | `/usr/share/foreman/default/` — seed rulebook, first-party worker types, launch-script bodies. Never edited. |
| `config/` templates | `/usr/share/foreman/config/` — shipped `foreman.toml`, empty `hooks/*.d/`, sample `extensions/foreman-menu.jsonc` |
| `~/.config/omarchy/` authoritative | `~/.config/foreman/` — `foreman.toml`, `workers/<id>/`, `hooks/`, `extensions/`, `rules/` |
| `~/.local/state/omarchy/` generated | `~/.local/state/foreman/` — roster, sessions, checkpoints, ledgers, merge ledger, inbox, slots, `toggles/`, `migrations/` markers |
| package.path state → config → package | a worker type resolves `~/.config/foreman/workers/<id>` over `/usr/share/foreman/default/workers/<id>` |
| `shell.json` replaces defaults wholesale, three-deep fallback | `foreman.toml`: a valid user file with `version = 1` replaces the packaged one entirely; bad parse → packaged → compiled-in literal. No merge, so a broken config never yields a half-configured swarm. |
| `refresh config` (backup + diff) | `foreman refresh config <p>`, same `.bak.<epoch>` + diff |
| `plugin clone` + IPC re-routing | `foreman worker clone muse` → `local.muse`; running jobs' references re-route to the clone; removing it falls back |

### Mutation surface — the `foreman <verb>` set

The CLI is the only writer to the state directory; each verb is `bin/foreman-<group>-<verb>`.

| Verb | Writes | Picked up by |
|---|---|---|
| `worker add <git-url>` / `clone` / `enable` / `disable` / `remove` / `list` / `validate` | `~/.config/foreman/workers/<id>/`; enabled-ness = presence in `foreman.toml` | collector `inotifywait` on the workers dir; `foreman restart collector` forces it |
| `slots set <model> <n>` / `slots show` | `state/slots.json`, written to a temp file and renamed | panel `FileView watchChanges`; launch scripts read it at the door |
| `reviewer set <component> <model>` | `foreman.toml` | collector config watch |
| `freeze` / `thaw` | creates/removes `state/toggles/frozen` — **presence is the state**, as with Hyprland toggles | launch scripts test it; panel watches the dir |
| `rule add <scope> <text>` | append-only rulings ledger | injected into the next spec by the launch scripts |
| `job launch` / `session relaunch` / `session kill` | roster + session record, minted id | collector observes the process |
| `restart collector` / `restart panel` | nothing | kill-and-relaunch with a readiness poll, `omarchy-restart-shell`'s shape |
| `hook install <type> <file>` | `~/.config/foreman/hooks/<type>.d/<name>`, chmod 755 | next hook fire |
| `migrate [--pending]` / `refresh config <p>` / `doctor` / `debug` | markers / user config / nothing | — |

Two mechanics to copy verbatim: **stage-then-rename** for any multi-file state change (theme-set builds `next-theme/` and `mv`s it over `current/theme`), and a **flock held over the critical section only**, released before the slow follow-on work (theme-set unlocks before the 15 retint scripts run). For Foreman: hold it while the roster/slot ledger is swapped, release before notifying the panel and firing hooks.

### Worker-type plugin manifest

A directory with `manifest.json` + launch script + skill. Required, mirroring Omarchy: `schemaVersion: 1, id, name, version, kinds[], entryPoints{}`, entry points being safe relative paths that exist and are not symlinks. Kinds: `worker | reviewer | supervisor | probe` (a probe is a headless observed-signal source — the analogue of Omarchy's `service`). Type block, the analogue of `barWidget`: `worker { displayName, model, vendor, defaultTimeoutMin, maxConcurrent, denyTools[], cannotTake[], usageMeterCmd, finishMarker, verdictPath }`, plus `defaults{}` and `schema[]` so the panel renders its settings without knowing the type. `foreman worker validate` mirrors the collector's checks exactly, for the reason Omarchy states: the CLI must refuse what the runtime would silently reject. Third-party types install as git checkouts, land **disabled**, show a diff on update, and the installer never runs their code. Panel extension gets the jsonc treatment: `~/.config/foreman/extensions/foreman-menu.jsonc`, dotted ids, `provider` rows for dynamic lists (pending approvals, stalled sessions).

### Hooks

`foreman-hook <name> [args]` → `~/.config/foreman/hooks/<name>` then `<name>.d/*`, skipping `*.sample`, failures logged not fatal. Points: **on-launch** (session id, role, model), **on-finish** (job id, exit status, verdict path), **on-land** (merge ledger line), **on-freeze** (transition), **on-alert** (anomaly type + payload from the collector's tick). Each fires *after* the state write is durable, as `omarchy-hook theme-set` fires after the atomic swap.

### Migrations

`/usr/share/foreman/migrations/<unix-timestamp>.sh` from the authoring commit; markers in `~/.local/state/foreman/migrations/`; `foreman migrate --pending` exits 0/1 as a condition; entries on fd 3 with `3<&-`. They may touch the state directory and `~/.config/foreman`, each opening with a guard that makes a re-run a no-op. Adopt the failure split: unrepairable → notice + `exit 0` so the queue drains; missing privilege or a locked state dir → nonzero, stays pending. Wait on the collector's lock the way `omarchy-migrate` waits on `db.lck`, and if still held after the deadline, exit 0 and retry next start. Run them at collector start and after package upgrade. Data files carry their own `version` and fall back to defaults on an unknown one rather than guessing.

### Discoverability and doctor

Same trick, no registry: `foreman` scans the first ~80 lines of each `bin/foreman-*` for `# foreman:summary= / group= / name= / args= / examples= / aliases= / hidden=`, derives the route from the filename, and reports collisions; `foreman commands --check` validates that metadata in CI. Keep Omarchy's narrow health verbs (`worker validate`, `migrate --pending`, `slots show`, `session stalled`) and a `foreman debug` dump (roster, last N ledger lines, collector log, vendor meter readings, process table for registered ids). Foreman should additionally ship the real `foreman doctor` that Omarchy lacks, because its failure mode is *silent divergence between declared and observed*: unregistered writer, orphan process holding no slot, checkpoint older than its session's last write, worktree with no session, slot ledger inconsistent with the process table.

## Files read

`bin/omarchy`; `/usr/bin/omarchy-{refresh-config, refresh-hyprland, theme-set, bar, plugin-clone, plugin-validate, shell, restart-shell, restart-hyprctl, migrate, dev-add-migration, hook, hook-install, reminder, debug, toggle-enabled, hyprland-toggle, cmd-missing, version}`; `default/hypr/{bootstrap.lua, omarchy.lua, require_optional.lua, toggles.lua}`; `config/hypr/hyprland.lua`; `config/omarchy/extensions/omarchy-menu.jsonc`; `default/omarchy/omarchy-menu.jsonc`; `shell/README.md`; `shell/shell.qml`; `shell/services/PluginRegistry.qml`; `shell/plugins/menu/Menu.qml`; `install/post-install/pacman.sh`; `migrations/{1788124236, 1788112314, 1780739888}.sh`; directory listings of `/usr/share/omarchy/{default,config,etc-overrides,themes,applications,migrations}`, `~/.config/omarchy`, `~/.local/state/omarchy`; output of `omarchy --help`, `omarchy {refresh,restart,plugin} --help`, `omarchy commands --check`. Foreman side: `README.md`, `docs/DESIGN.md`, `docs/research/omarchy-shell-surfaces.md`.
