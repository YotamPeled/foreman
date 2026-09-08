# Omarchy shell surfaces for Foreman

Installed Omarchy: `$OMARCHY_PATH=/usr/share/omarchy`. Reference plugin: `/mnt/ssd/projects/omarchy-agent-workspaces`.

## 1. Plugin kinds

CONFIRMED (local `shell/README.md`, `shell/plugins/README.md`, `services/PluginRegistry.qml`; matches `omarchy.org/manual/shell-plugins/` and `plugins.omarchy.org/develop.html`). Six kinds, one manifest per plugin, `kinds: []` can list several:

| kind | what | entry point |
|---|---|---|
| `bar-widget` | item the active bar drops into a section | `barWidget` |
| `panel` | persistent or summoned floating window (e.g. OSD) | `panel` |
| `overlay` | fullscreen surface (e.g. emoji/clipboard picker) | `overlay` |
| `menu` | summoned menu surface | `menu` |
| `service` | headless singleton, no UI | `service` |
| `bar` | full bar replacement (only one active at a time) | `bar` |

Manifest required fields: `schemaVersion:1, id, name, version, kinds, entryPoints`. Our own manifest (`omarchy-agent-workspaces/manifest.json`) is a minimal `bar-widget` example: `"kinds":["bar-widget"], "entryPoints":{"barWidget":"Workspaces.qml"}` plus a `barWidget:{displayName,description,category,defaultSection,allowMultiple}` block. `keepLoaded:true` (seen on `omarchy.osd`, `omarchy.menu`, `omarchy.emojis`) keeps a panel/overlay/menu's window mounted between summons instead of tearing it down.

## 2. Full-screen / layer-shell surface toggled by keybinding

CONFIRMED. `panel`/`overlay`/`menu` plugins build a Quickshell `PanelWindow` with `WlrLayershell` attached, e.g. `shell/plugins/emojis/Emojis.qml`:
```qml
PanelWindow {
  WlrLayershell.namespace: "omarchy-emojis"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
}
```
Same pattern in `shell/plugins/menu/Menu.qml` (`WlrLayershell.namespace:"omarchy-menu"`, full-screen anchors top/bottom/left/right, `WlrKeyboardFocus.Exclusive`). Omarchy's own root menu (`omarchy-menu`) and settings/panels (network, audio, bluetooth, power, agents) are the same mechanism: a plugin registers `summon`/`hide`/`toggle` on the shell's single IPC target, and a Hyprland keybind just shells out to it. Confirmed shipped (commented) example in `config/hypr/bindings.lua`:
```
o.bind("SUPER + PERIOD", nil, "omarchy-shell shell toggle omarchy.emojis")
```
This is the **generic IPC path**, not first-party-only — `shell/README.md`'s IPC table (`summon <id> <payload>`, `hide <id>`, `toggle <id> <payload>`) applies to any plugin id, first- or third-party. A user plugin exposing `kind:"panel"|"overlay"|"menu"` gets a real layer-shell surface toggled the identical way: `omarchy-shell shell toggle <our-id> '{}'` bound to a key in `~/.config/hypr/bindings.lua`.

## 3. Data feed into a plugin

CONFIRMED from Quickshell docs (`quickshell.org/docs/master/types/Quickshell.Io/`) and local code: `FileView` (file read + `watchChanges:true` watcher, no polling), `Process` with `stdout`/`stderr` as `DataStreamParser` (`SplitParser` for line-delimited, `StdioCollector` for buffered whole-output), sockets not used by any first-party plugin found.

`omarchy-agent-workspaces/Workspaces.qml` uses exactly this: three `FileView { watchChanges:true; printErrors:false }` blocks reading the Python helper's JSON state files (`sessions.json`, `needs.json`, `slugs.json` under `~/.local/state/omarchy/agent-workspaces/`), two `Process` blocks (one to shell out for title/slug work), and a `Timer{interval:2000; repeat:true}` polling loop (`root.scanTitles()`) alongside the file watch, plus a fast one-shot `Timer{interval:60}` for debounce. The `omarchy.agents` first-party plugin (closest domain analog) defaults `refreshIntervalSec:900` for its own polling. Norm: file-watch for push-style state, a short (1–5s) `Timer` poll only where the source has no watchable file, longer (minutes) polling for network/API calls.

## 4. Theming

CONFIRMED. Two QML singletons under `qs.Commons` (import `import qs.Commons`), both `pragma Singleton`, declared in `shell/Commons/qmldir`:
- **`Color`** (`shell/Commons/Color.qml`) — `Color.foreground`, `Color.background`, `Color.accent`, `Color.urgent`, `Color.muted`, generated from `theme/colors.toml`; reassigns its `shellValues` dict wholesale on theme change so every binding re-evaluates.
- **`Style`** (`shell/Commons/Style.qml`) — structural tokens: `Style.font.*` (family/body/caption/display sizes), `Style.spacing.*`, `Style.space(n)`, `cornerRadius`, `gapsOut`, plus derived state colors (`hoverFill`, `selectedBorderColor`, etc.) built from `Color.*`.

Both re-derive automatically on `omarchy theme set` (re-read `theme/shell.toml`/`colors.toml`, reassign properties) — no plugin action needed beyond binding to `Color.*`/`Style.*` instead of hardcoding. `Workspaces.qml` follows this: `Color.foreground` as fallback, `Style.space(24)`, `Style.font.family` bound directly.

## 5. Notifications with action buttons

PARTIALLY CONFIRMED, with a real limitation. The daemon (`shell/plugins/notifications/Service.qml`) sets `actionsSupported: true` in its DBus capabilities and does invoke a **single "default" action** on click (`invokePopupDefault`, walks `ref.actions[]` for `identifier === "default"`). But `components/NotificationCard.qml` (196 lines, checked in full) has **no button/action-row rendering at all** — no `Repeater` over an actions array, only `onCardClicked → invokePopupDefault`. The first-party helper `omarchy-notification-send` (`bin/omarchy-notification-send`) calls `org.freedesktop.Notifications.Notify` directly via `busctl` and hard-codes an **empty actions array** (`0` count); its only "click" mechanism is a custom hint `omarchy-exec-argv` (from `--exec program args...`) fired when the whole toast is clicked — one action, not labeled buttons.
CONCLUSION: a Foreman notification can carry one click-through action (approve *or* open, chosen at send time), not multiple distinct buttons ("approve" / "write rule" as separate clickables) — UNVERIFIED whether a raw multi-action `Notify` call (bypassing the helper, with a real `actions` array) would render buttons, since the card component has no code path to draw them; the evidence above suggests it would not.

## 6. Menu extension (`omarchy-menu.jsonc`)

CONFIRMED. `~/.config/omarchy/extensions/omarchy-menu.jsonc` (parsed with `watchChanges:true`, live-reload, alongside shipped defaults in `default/omarchy/omarchy-menu.jsonc`). Entries are JSONC object keys; dotted id sets parent (`"foreman.approve"` nests under `"foreman"`). Per-entry fields: `icon` (Nerd Font glyph), `label`, `action` (shell command; omit for a submenu), `target` (link to an existing submenu id), `provider` (a function/command returning JSON rows, for dynamic lists), `aliases` (extra `omarchy menu summon <name>` routes), `description`, `when` (shell condition to hide), `checked` (shell condition to show a ✓). Reusing an existing id overrides/extends it. This gives Foreman a static or **dynamic** (`provider`) submenu (e.g. list of pending agent approvals) driven by a shell command, with `action:` running arbitrary commands per row.

## 7. Reusable list/card/badge components

CONFIRMED, under `shell/Ui/` (import `qs.Ui`), all theme-bound via `Color`/`Style`:
- `PopupCard.qml` — themed `PopupWindow` anchored to a bar widget (the exact shape of the `omarchy.agents` panel popup: `anchorItem`, `bar`, `contentWidth/Height`, `Style.spacing.popupPadding`).
- `PanelHero.qml` — icon + title/meta/detail header block for the top of a panel.
- `PanelSectionHeader.qml` / `PanelSeparator.qml` — small-caps section label + 1px divider, used to lay out grouped rows (seen driving "DNS provider", "Wi-Fi networks" style lists).
- `ButtonGroup.qml` — segmented "pick one of N" control (keyboard nav built in).
- `WidgetButton.qml` / `BarIndicator.qml` / `BarIconButton.qml` — bar-chrome buttons/badges with an `active`/urgent-tinted state, the badge look used for e.g. mic/DND indicators.
- `ConfirmDialog.qml` — themed yes/no modal (cancel/confirm, keyboard-navigable) — closest existing primitive to an "approve" prompt.
- `Dropdown.qml` / `SearchableDropdown.qml` / `MultiSelect.qml`, `Toggle.qml`/`ToggleSwitch.qml`, `NumberField.qml`/`TextField.qml` — form controls, all theme-bound.
No ready-made scrollable list/table/card-grid component was found; panels compose rows manually from `PanelSectionHeader`/`PanelSeparator`/buttons (see `agents/Panel.qml`, `panels/network/Panel.qml`).

## What Foreman can use natively, ranked by shell-machinery reuse

1. **Bar widget + popup panel** (`kind:"bar-widget"`, `PopupCard.qml` popup) — exactly the `omarchy.agents` pattern: one bar icon/badge for swarm status, click opens a themed popup built from `PanelHero`/`PanelSectionHeader`/`WidgetButton`. Reuses the most first-party UI kit and needs no new IPC/keybinding wiring — this is what `omarchy-agent-workspaces` already half-does.
2. **Panel/overlay plugin summoned by keybinding** — a full `PanelWindow`+`WlrLayershell` surface (Foreman's main board/log view), toggled with `omarchy-shell shell toggle <id>` bound in `bindings.lua`, exactly like `omarchy-menu`/emoji picker/OSD. Full control over layout, still theme-bound via `Color`/`Style`.
3. **`omarchy-menu.jsonc` submenu** — a `provider`-backed dynamic list of pending approvals/agents reachable from the root Omarchy menu; cheapest to build (no QML), but read-only/action-per-row, not a live dashboard.
4. **Notification** — good for one-shot low-friction pings with a single click-through action; not viable for multi-button approve/deny without further verification (see §5), so treat as a supplement to (1)/(2), not the approval surface itself.
5. **Service plugin** (`kind:"service"`) — headless singleton for background polling/state aggregation, feeding (1)/(2)/(3) rather than a surface of its own.

## Verified sources

Local (this machine):
- `/usr/share/omarchy/shell/README.md`
- `/usr/share/omarchy/shell/plugins/README.md`
- `/usr/share/omarchy/shell/services/PluginRegistry.qml`
- `/usr/share/omarchy/shell/services/BarWidgetRegistry.qml`
- `/usr/share/omarchy/config/omarchy/shell.json`
- `/home/yotam/.config/omarchy/shell.json`
- `/usr/share/omarchy/config/omarchy/extensions/omarchy-menu.jsonc`
- `/usr/share/omarchy/shell/Commons/Style.qml`
- `/usr/share/omarchy/shell/Commons/Color.qml`
- `/usr/share/omarchy/shell/Commons/qmldir`
- `/usr/share/omarchy/shell/plugins/osd/Osd.qml`, `osd/manifest.json`
- `/usr/share/omarchy/shell/plugins/menu/Menu.qml`, `menu/manifest.json`
- `/usr/share/omarchy/shell/plugins/emojis/Emojis.qml`, `emojis/manifest.json`
- `/usr/share/omarchy/shell/plugins/agents/manifest.json`, `agents/Panel.qml`
- `/usr/share/omarchy/shell/plugins/notifications/Service.qml`, `notifications/components/NotificationCard.qml`
- `/usr/share/omarchy/bin/omarchy-notification-send`, `bin/omarchy-notification-wait`
- `/usr/share/omarchy/config/hypr/bindings.lua`, `/home/yotam/.config/hypr/bindings.lua`
- `/usr/share/omarchy/shell/Ui/PopupCard.qml`, `PanelHero.qml`, `PanelSectionHeader.qml`, `PanelSeparator.qml`, `ButtonGroup.qml`, `WidgetButton.qml`, `ConfirmDialog.qml`, `Dropdown.qml`, `BarIndicator.qml`
- `/mnt/ssd/projects/omarchy-agent-workspaces/manifest.json`, `Workspaces.qml`, `bin/agent-ws`

Online:
- https://omarchy.org/manual/shell-plugins/
- https://plugins.omarchy.org/develop.html
- https://plugins.omarchy.org/publish.html
- https://github.com/basecamp/omarchy/tree/quattro/shell
- https://github.com/basecamp/omarchy/tree/quattro/shell/plugins
- https://quickshell.org/docs/v0.1.0/types/Quickshell/PanelWindow/
- https://quickshell.org/docs/v0.1.0/types/Quickshell.Wayland/WlrLayershell/
- https://quickshell.org/docs/v0.1.0/types/Quickshell.Wayland/WlrLayer/
- https://quickshell.org/docs/master/types/Quickshell.Io/
- https://quickshell.org/docs/v0.1.0/types/Quickshell.Io/Process/
- https://quickshell.org/docs/master/types/Quickshell.Io/FileView/
