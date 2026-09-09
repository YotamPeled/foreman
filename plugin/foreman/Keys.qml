pragma Singleton
pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io
import "." as Foreman

// Every key on the panel runs a real `foreman` verb through the Process
// below; nothing here writes a ledger directly. Two kinds:
//
// Static keys, fixed and always the same: F freezes everything,
// ? opens the cheat sheet, Esc backs out one level and closes the panel
// at the top. These are the mock's footer and they never move.
//
// Two-letter hints for everything else, assigned on appearance: every
// actionable row — each Needs you approve and decline, each Problem's
// action — gets a hint when it appears, keeps it while it is on screen,
// and releases it when the row goes. Typing the first letter narrows to
// the matching hints; the second fires. The hint alphabet excludes `f`
// so a hint never collides with the static freeze key.
//
// The chips in the blocks bind hintFor(); Panel.qml forwards key presses
// to handleText()/handleEscape(); the headless keys harness drives the
// same two functions and waits on done().
QtObject {
  id: root

  // The hint alphabet: a-z without f, so no hint starts with the freeze
  // letter. 25 letters give 625 two-letter hints, far past any screen;
  // that product is also the capacity: a row past it gets no hint
  // rather than a recycled or malformed one, and a hintless row fires
  // through no key.
  readonly property string alphabet: "abcdeghijklmnopqrstuvwxyz"
  readonly property int hintCapacity: 625

  // action id -> two-letter hint, reassigned only by syncHints().
  property var hints: ({})
  // First letter of a two-letter hint while the panel waits for the
  // second, "" otherwise.
  property string pending: ""
  // The ? cheat sheet overlay in Panel.qml reads this.
  property bool cheatOpen: false

  // One verb at a time: fire() appends, pump() starts the head while the
  // runner is idle, onRunnerExited() logs, emits done() and pumps next.
  property var queue: []
  property string currentId: ""

  signal done(string actionId, int exitCode)

  function validHint(hint) {
    return /^[a-e,g-z][a-e,g-z]$/.test(String(hint || ""))
  }

  function hintForIndex(n) {
    var alpha = root.alphabet
    var base = alpha.length
    return alpha[Math.floor(n / base)] + alpha[n % base]
  }

  function approveText(row) {
    var opts = (row && row.options) || []
    return opts.length > 0 ? String(opts[0]) : "yes"
  }

  function declineText(row) {
    var opts = (row && row.options) || []
    return opts.length > 1 ? String(opts[1]) : "no"
  }

  // Split an anomaly's "foreman ..." action into the argv the Process
  // runs: ["relaunch", "ses-..."], ["kill", "pid-..."] or
  // ["job", "kill", "job-..."]. The panel runs the row's verb verbatim;
  // where the CLI has no such verb fire() logs KEY-NOVERB first and the
  // run fails, rather than substituting another verb.
  function argvForAction(action) {
    var s = String(action || "").trim().replace(/^\s*foreman\s+/i, "")
    if (s === "") return []
    return s.split(/\s+/)
  }

  // Every actionable row in model order: each open inbox item's approve
  // then decline, then each open anomaly that carries an action.
  function currentActions() {
    var out = []
    var need = Foreman.Model.needsYou
    var rows = (need && need.rows) ? need.rows : []
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i]
      if (!row || !row.id) continue
      var approve = root.approveText(row)
      var decline = root.declineText(row)
      out.push({ id: "answer:" + row.id + ":approve", verb: ["answer", row.id, approve],
                 label: "answer " + row.id + " " + approve + " (approve)" })
      out.push({ id: "answer:" + row.id + ":decline", verb: ["answer", row.id, decline],
                 label: "answer " + row.id + " " + decline + " (decline)" })
    }
    var prob = Foreman.Model.problems
    var prows = (prob && prob.rows) ? prob.rows : []
    for (var k = 0; k < prows.length; k++) {
      var p = prows[k]
      if (!p || !p.action) continue
      var argv = root.argvForAction(p.action)
      if (argv.length === 0) continue
      out.push({ id: "problem:" + (p.kind || "") + ":" + (p.subject || ""),
                 verb: argv, label: String(p.action) })
    }
    return out
  }

  // Keep assigned hints stable while their rows stay on screen, hand the
  // next free hint to each new row in model order, release the rest.
  // Runs off Model.changed, never from inside a hintFor() binding.
  //
  // A pending first letter narrows to the hints on screen when it was
  // typed. When the resync changes that letter's candidate mapping —
  // a hint released, reused or added under it — the pending letter is
  // cancelled instead of letting the second letter resolve against the
  // new map and fire a row the owner was not looking at.
  function syncHints() {
    var acts = root.currentActions()
    var waiting = root.pending
    var before = ({})
    if (waiting !== "") {
      var oldIds = Object.keys(root.hints)
      for (var b = 0; b < oldIds.length; b++) {
        var oldHint = root.hints[oldIds[b]]
        if (oldHint && oldHint[0] === waiting) before[oldIds[b]] = oldHint
      }
    }
    var next = ({})
    var used = ({})
    for (var i = 0; i < acts.length; i++) {
      var keep = root.hints[acts[i].id]
      if (keep && !used[keep] && root.validHint(keep)) {
        next[acts[i].id] = keep
        used[keep] = true
      }
    }
    var n = 0
    for (var j = 0; j < acts.length; j++) {
      if (next[acts[j].id]) continue
      if (n >= root.hintCapacity) {
        next[acts[j].id] = ""
        continue
      }
      while (n < root.hintCapacity && used[root.hintForIndex(n)]) n++
      if (n >= root.hintCapacity) {
        next[acts[j].id] = ""
        continue
      }
      next[acts[j].id] = root.hintForIndex(n)
      used[next[acts[j].id]] = true
      n++
    }
    if (waiting !== "") {
      var changed = false
      var after = ({})
      for (var a = 0; a < acts.length; a++) {
        var newHint = next[acts[a].id]
        if (newHint && newHint[0] === waiting) after[acts[a].id] = newHint
      }
      var beforeIds = Object.keys(before)
      var afterIds = Object.keys(after)
      if (beforeIds.length !== afterIds.length) changed = true
      else {
        for (var c = 0; c < afterIds.length; c++)
          if (before[afterIds[c]] !== after[afterIds[c]]) { changed = true; break }
      }
      if (changed) root.pending = ""
    }
    var same = true
    var oldKeys = Object.keys(root.hints)
    var newKeys = Object.keys(next)
    if (oldKeys.length !== newKeys.length) same = false
    else {
      for (var k = 0; k < newKeys.length; k++)
        if (root.hints[newKeys[k]] !== next[newKeys[k]]) { same = false; break }
    }
    if (!same) root.hints = next
  }

  function hintFor(actionId) {
    return root.hints[actionId] || ""
  }

  // While a first letter is pending, chips opening with another letter
  // dim: the narrowing the spec asks for.
  function dimFor(hint) {
    if (root.pending === "") return false
    var h = String(hint || "")
    return h.length !== 2 || h[0] !== root.pending
  }

  function actionById(actionId) {
    var acts = root.currentActions()
    for (var i = 0; i < acts.length; i++)
      if (acts[i].id === actionId) return acts[i]
    return null
  }

  // Verbs this runtime does not ship: `kill` was never added to the CLI
  // and `job` offers only verify and fail, so a row asking for either
  // cannot run. The key still attempts the verb verbatim — the CLI is
  // the only way the panel mutates anything — but it says so first
  // rather than failing silently.
  function verbMissing(argv) {
    if (argv.length === 0) return false
    if (argv[0] === "kill") return true
    if (argv[0] === "job" && argv.length > 1 && argv[1] === "kill") return true
    return false
  }

  function fire(actionId) {
    var act = root.actionById(actionId)
    if (!act) {
      console.log("KEY-SKIP " + actionId + " gone")
      root.done(actionId, 127)
      return false
    }
    if (root.verbMissing(act.verb))
      console.log("KEY-NOVERB " + actionId + " foreman " + act.verb.join(" ")
                  + " is absent from this runtime; attempting verbatim")
    root.queue.push({ id: actionId, argv: act.verb })
    console.log("KEY-FIRE " + actionId + " foreman " + act.verb.join(" "))
    root.pump()
    return true
  }

  function fireFreeze() {
    var argv = ["freeze"]
    root.queue.push({ id: "static:freeze", argv: argv })
    console.log("KEY-FIRE static:freeze foreman " + argv.join(" "))
    root.pump()
  }

  function pump() {
    if (runner.running) return
    if (root.queue.length === 0) return
    var head = root.queue[0]
    root.currentId = head.id
    runner.command = ["env", "-u", "FOREMAN_SESSION", "foreman"].concat(head.argv)
    runner.running = true
  }

  function onRunnerExited(code) {
    var id = root.currentId
    root.currentId = ""
    if (root.queue.length > 0 && root.queue[0].id === id) root.queue.shift()
    console.log("KEY-DONE " + id + " " + code)
    root.done(id, code)
    root.pump()
  }

  // The single dispatch Panel.qml's key handler and the headless harness
  // both call. Returns what happened: cheat-open, freeze, pending,
  // fired:<id>, invalid or ignored.
  function handleText(text) {
    var t = String(text || "")
    if (t === "?") {
      root.cheatOpen = true
      root.pending = ""
      return "cheat-open"
    }
    if (t === "f" || t === "F") {
      root.pending = ""
      root.fireFreeze()
      return "freeze"
    }
    var lower = t.toLowerCase()
    if (!/^[a-z]$/.test(lower) || lower === "f") return "ignored"
    var acts = root.currentActions()
    if (root.pending === "") {
      for (var i = 0; i < acts.length; i++) {
        var h = root.hints[acts[i].id] || ""
        if (h !== "" && h[0] === lower) {
          root.pending = lower
          return "pending"
        }
      }
      return "ignored"
    }
    var combo = root.pending + lower
    root.pending = ""
    for (var j = 0; j < acts.length; j++) {
      if ((root.hints[acts[j].id] || "") === combo) {
        root.fire(acts[j].id)
        return "fired:" + acts[j].id
      }
    }
    return "invalid"
  }

  // Esc backs out one level: out of the cheat sheet, out of a pending
  // first letter, and only at the top out of the panel itself.
  function handleEscape() {
    if (root.cheatOpen) {
      root.cheatOpen = false
      return "cheat-closed"
    }
    if (root.pending !== "") {
      root.pending = ""
      return "pending-cleared"
    }
    return "close-panel"
  }

  // hint + label per dynamic action, in model order, for the cheat sheet.
  function cheatActions() {
    var acts = root.currentActions()
    var out = []
    for (var i = 0; i < acts.length; i++)
      out.push({ hint: root.hints[acts[i].id] || "", label: acts[i].label })
    return out
  }

  // The panel acts for the owner, never for whatever session launched
  // the shell: an inherited FOREMAN_SESSION names a writer the ledger
  // does not know and the verb is refused. The runner strips it from
  // the verb's environment, so every key lands as the owner.
  property Process runner: Process {
    running: false
    onExited: (code, signal) => { root.onRunnerExited(code) }
  }

  // Model.changed carries every rebuild, so hints track the rows without
  // a poll of our own. Connected imperatively: a Connections element has
  // no default property to hold the handler in this Qt build.
  Component.onCompleted: {
    Foreman.Model.changed.connect(root.syncHints)
    root.syncHints()
  }
}
