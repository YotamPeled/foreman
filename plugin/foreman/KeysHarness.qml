import Quickshell
import QtQuick
import "." as Foreman

// Headless driver for `bin/foreman-verify panel --keys`: the panel's own
// Keys singleton against a throwaway copy of the fixture (FOREMAN_STATE
// points at the copy), driven through the same handleText()/handleEscape()
// entry points Panel.qml's key handler calls. Each fire runs the real
// `foreman` verb through Keys' Process and reports its exit code:
//
//   HINT <hint> <action-id> <argv-json>
//   NEEDSYOU-BEFORE <n> / FROZEN-BEFORE <bool>
//   CHEAT-OPEN <bool> / CHEAT-ESC <result>
//   PENDING <letter> / PENDING-ESC <result>
//   QUEUE <id,...>            the fires, in order
//   KEY-FIRE / KEY-DONE       from Keys, one per fire
//   NEEDSYOU-AFTER <n> / FROZEN-AFTER <bool>
//   HARNESS-DONE settled|deadline
//
// The queue answers one option per inbox item (approve the first, decline
// the second), so both answer variants are proven without answering one
// item twice. The Problems fires attempt their rows' verbs verbatim; where
// the CLI has no such verb the exit code says so and the Python half marks
// the key unproven rather than substituting another verb.
ShellRoot {
  id: harness

  property string signature: ""
  property int stable: 0
  property int ticks: 0
  property int phase: 0 // 0 settle, 1 driving, 2 tail, 3 reported
  property var queue: []
  property int qi: 0
  property int tailTicks: 0

  readonly property int settleTicks: 4    // 4 x 250ms unchanged
  readonly property int deadlineTicks: 40 // 10s, then print regardless
  // A hung verb must fail fast naming the key, never stall the check
  // past the runner's own timeout: whatever phase, give up here.
  readonly property int hardTicks: 160 // 40s, then print regardless

  function counts() {
    var names = Foreman.Model.blockNames
    var out = []
    for (var i = 0; i < names.length; i++)
      out.push(names[i] + " " + Foreman.Model.block(names[i]).count)
    return out
  }

  // The mock fixture's hand-derived counts (bin/foreman-verify EXPECTED):
  // settling before every watched file has delivered means assigning
  // hints to a half-loaded model, so stability alone is not enough.
  // This harness only ever runs against a throwaway copy of that fixture.
  function ready() {
    return Foreman.Model.needsYou.count === 2
        && Foreman.Model.problems.count === 3
  }

  function hintSig() {
    var acts = Foreman.Keys.currentActions()
    var parts = []
    for (var i = 0; i < acts.length; i++)
      parts.push(acts[i].id + "=" + (Foreman.Keys.hintFor(acts[i].id) || "-"))
    return parts.join(",")
  }

  function reportHints() {
    var acts = Foreman.Keys.currentActions()
    for (var i = 0; i < acts.length; i++) {
      var h = Foreman.Keys.hintFor(acts[i].id) || ""
      console.log("HINT " + h + " " + acts[i].id + " " + JSON.stringify(acts[i].verb))
    }
  }

  function buildQueue() {
    var q = ["static:freeze"]
    var need = Foreman.Model.needsYou.rows || []
    for (var i = 0; i < need.length; i++) {
      if (!need[i] || !need[i].id) continue
      q.push("answer:" + need[i].id + ":" + (i % 2 === 0 ? "approve" : "decline"))
    }
    var acts = Foreman.Keys.currentActions()
    for (var j = 0; j < acts.length; j++) {
      if (acts[j].id.indexOf("problem:") === 0) q.push(acts[j].id)
    }
    return q
  }

  function fireNext() {
    if (harness.qi >= harness.queue.length) {
      harness.phase = 2
      harness.tailTicks = 0
      return
    }
    var id = harness.queue[harness.qi]
    if (id === "static:freeze") Foreman.Keys.fireFreeze()
    else Foreman.Keys.fire(id)
  }

  function onActionDone(id, code) {
    if (harness.phase !== 1) return
    harness.qi += 1
    harness.fireNext()
  }

  function report(settled) {
    console.log("NEEDSYOU-AFTER " + Foreman.Model.needsYou.count)
    console.log("FROZEN-AFTER " + Foreman.Model.frozen)
    console.log(settled ? "HARNESS-DONE settled" : "HARNESS-DONE deadline")
    Qt.exit(0)
  }

  Component.onCompleted: {
    Foreman.Keys.done.connect(harness.onActionDone)
  }

  Timer {
    interval: 250
    running: true
    repeat: true
    onTriggered: {
      harness.ticks += 1
      if (harness.phase !== 0 && harness.phase !== 3
          && harness.ticks >= harness.hardTicks) {
        harness.phase = 3
        harness.report(false)
      } else if (harness.phase === 0) {
        var current = harness.counts().join(",") + "\n" + harness.hintSig()
        if (current === harness.signature) harness.stable += 1
        else { harness.signature = current; harness.stable = 0 }
        if (harness.ready() && harness.ticks >= harness.settleTicks
            && harness.stable >= harness.settleTicks) {
          Foreman.Keys.syncHints()
          harness.reportHints()
          console.log("NEEDSYOU-BEFORE " + Foreman.Model.needsYou.count)
          console.log("FROZEN-BEFORE " + Foreman.Model.frozen)
          Foreman.Keys.handleText("?")
          console.log("CHEAT-OPEN " + Foreman.Keys.cheatOpen)
          console.log("CHEAT-ESC " + Foreman.Keys.handleEscape())
          var acts = Foreman.Keys.currentActions()
          if (acts.length > 0) {
            var h0 = Foreman.Keys.hintFor(acts[0].id) || ""
            if (h0 !== "") {
              Foreman.Keys.handleText(h0[0])
              console.log("PENDING " + Foreman.Keys.pending)
              console.log("PENDING-ESC " + Foreman.Keys.handleEscape())
            }
          }
          harness.queue = harness.buildQueue()
          console.log("QUEUE " + harness.queue.join(","))
          harness.phase = 1
          harness.qi = 0
          harness.fireNext()
        } else if (harness.ticks >= harness.deadlineTicks) {
          harness.phase = 3
          console.log("HARNESS-DONE deadline")
          Qt.exit(0)
        }
      } else if (harness.phase === 2) {
        harness.tailTicks += 1
        // ~3s for the model's 2s poll to re-read the ledgers the verbs
        // wrote, so NEEDSYOU-AFTER is the panel reflecting the change.
        if (harness.tailTicks >= 12) {
          harness.phase = 3
          harness.report(true)
        }
      }
    }
  }
}
