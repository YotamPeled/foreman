import Quickshell
import Quickshell.Io
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
//   QUEUE <id,...>            the typed fires, in order
//   KEY-FIRE / KEY-DONE       from Keys, one per fire
//   WRONGROW-PENDING <result>       first letter typed on the settled map
//   WRONGROW-CANCELLED <bool>       pending cleared when a new row arrived
//   WRONGROW-NOFIRE <bool>          the second letter fired nothing
//   WRONGROW-FIRED <result>         both letters typed on the new map
//   NEEDSYOU-AFTER <n> / FROZEN-AFTER <bool>
//   HARNESS-DONE settled|deadline
//
// The queue answers one option per inbox item (approve the first, decline
// the second), so both answer variants are proven without answering one
// item twice. Every fire is typed: F through handleText, each dynamic row
// through both letters of its current hint. The Problems fires attempt
// their rows' verbs verbatim: the relaunch the fixture cannot run stays
// unproven, while the kill the runtime ships must succeed and the Python
// half asserts the ledger lines it wrote.
//
// After the queue drains, the wrong-row probe types a first letter, adds
// a row underneath through `foreman ask` on a separate Process, and
// checks the panel cancelled the pending letter; then it types the
// second letter and checks nothing fired — never the new row. Finally
// it types the new row's full hint and checks the right row fires, and
// answers it so the ledger ends as the fixture holds it plus one probe.
ShellRoot {
  id: harness

  property string signature: ""
  property int stable: 0
  property int ticks: 0
  property int phase: 0 // 0 settle, 1 driving, 2 tail, 3 probe, 4 tail, 5 reported
  property var queue: []
  property int qi: 0
  property int tailTicks: 0
  property int fires: 0
  property int probeStep: 0 // 0 idle, 1 ask running, 2 settling, 4 awaiting probe fire
  property int probeWait: 0
  property int probeFires: 0
  property int probeAskCode: -1
  property string probeAction: ""

  readonly property int settleTicks: 4    // 4 x 250ms unchanged
  readonly property int deadlineTicks: 40 // 10s, then print regardless
  // A hung verb must fail fast naming the key, never stall the check
  // past the runner's own timeout: whatever phase, give up here.
  readonly property int hardTicks: 160 // 40s, then print regardless
  readonly property int probeSettleTicks: 16 // 4s for the ask to land
  readonly property int probeFireTicks: 20   // 5s for the probe answer

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

  // Every fire through the panel's real text entry point: the hint is
  // read off the current map and both letters are typed, so dispatch —
  // the first letter, the second, the static keys — is what is proven.
  function typeId(id) {
    if (id === "static:freeze") {
      Foreman.Keys.handleText("F")
      return
    }
    var h = Foreman.Keys.hintFor(id) || ""
    if (h.length !== 2) {
      console.log("KEY-SKIP " + id + " no hint")
      return
    }
    Foreman.Keys.handleText(h[0])
    Foreman.Keys.handleText(h[1])
  }

  function fireNext() {
    if (harness.qi >= harness.queue.length) {
      harness.phase = 2
      harness.tailTicks = 0
      return
    }
    harness.typeId(harness.queue[harness.qi])
  }

  function onActionDone(id, code) {
    harness.fires += 1
    if (harness.phase === 1) {
      harness.qi += 1
      harness.fireNext()
    } else if (harness.phase === 3 && harness.probeStep === 4
               && id === harness.probeAction) {
      harness.finishProbe()
    }
  }

  // The wrong-row probe: a first letter is pending when a new row lands
  // underneath. The panel must cancel the pending letter so the second
  // letter cannot resolve against the new map.
  function startProbe() {
    harness.probeFires = harness.fires
    var r = Foreman.Keys.handleText("a")
    console.log("WRONGROW-PENDING " + r)
    harness.probeStep = 1
    harness.probeProc.command = ["env", "-u", "FOREMAN_SESSION", "foreman",
                                 "ask", "Do the panel hints survive rows changing underneath?",
                                 "--kind", "scope", "--recommend", "yes"]
    harness.probeProc.running = true
  }

  function onProbeAskExited(code) {
    harness.probeAskCode = code
    if (harness.probeStep === 1) {
      harness.probeStep = 2
      harness.probeWait = 0
    }
  }

  // The probe row is the one inbox item the fixture never held.
  function probeOpenId() {
    var rows = Foreman.Model.needsYou.rows || []
    for (var i = 0; i < rows.length; i++) {
      var rid = (rows[i] && rows[i].id) || ""
      if (rid !== "" && rid !== "inb-tokens01" && rid !== "inb-reaper01"
          && rid !== "inb-theme001") return rid
    }
    return ""
  }

  function evaluateProbe() {
    var cancelled = Foreman.Keys.pending === ""
    var nofire = harness.fires === harness.probeFires
    // The second letter, typed after the map changed: with the pending
    // letter cancelled this only re-narrows and fires nothing.
    Foreman.Keys.handleText("a")
    nofire = nofire && harness.fires === harness.probeFires
    console.log("WRONGROW-CANCELLED " + cancelled)
    console.log("WRONGROW-NOFIRE " + nofire)
    var pid = harness.probeOpenId()
    if (harness.probeAskCode !== 0 || pid === "") {
      console.log("WRONGROW-FIRED missing")
      harness.finishProbe()
      return
    }
    harness.probeAction = "answer:" + pid + ":approve"
    var h = Foreman.Keys.hintFor(harness.probeAction) || ""
    if (h.length !== 2) {
      console.log("WRONGROW-FIRED missing")
      harness.finishProbe()
      return
    }
    Foreman.Keys.handleEscape()
    var first = Foreman.Keys.handleText(h[0])
    var second = ""
    if (first === "pending") second = Foreman.Keys.handleText(h[1])
    console.log("WRONGROW-FIRED " + second)
    harness.probeStep = 4
    harness.probeWait = 0
  }

  function finishProbe() {
    Foreman.Keys.handleEscape()
    harness.probeStep = 0
    harness.phase = 4
    harness.tailTicks = 0
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

  // The model change underneath the pending letter: a real `foreman`
  // verb on a separate Process, never a direct ledger write.
  property Process probeProc: Process {
    running: false
    onExited: (code, signal) => { harness.onProbeAskExited(code) }
  }

  Timer {
    interval: 250
    running: true
    repeat: true
    onTriggered: {
      harness.ticks += 1
      if (harness.phase !== 0 && harness.phase !== 5
          && harness.ticks >= harness.hardTicks) {
        harness.phase = 5
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
          harness.phase = 5
          console.log("HARNESS-DONE deadline")
          Qt.exit(0)
        }
      } else if (harness.phase === 2) {
        harness.tailTicks += 1
        // ~3s for the model's 2s poll to re-read the ledgers the verbs
        // wrote, so NEEDSYOU-AFTER is the panel reflecting the change.
        if (harness.tailTicks >= 12) {
          harness.phase = 3
          harness.startProbe()
        }
      } else if (harness.phase === 3) {
        if (harness.probeStep === 2) {
          harness.probeWait += 1
          if (harness.probeWait >= harness.probeSettleTicks) harness.evaluateProbe()
        } else if (harness.probeStep === 4) {
          harness.probeWait += 1
          if (harness.probeWait >= harness.probeFireTicks) {
            console.log("WRONGROW-FIRED timeout")
            harness.finishProbe()
          }
        }
      } else if (harness.phase === 4) {
        harness.tailTicks += 1
        // ~3s for the probe answer to land, so NEEDSYOU-AFTER counts it.
        if (harness.tailTicks >= 12) {
          harness.phase = 5
          harness.report(true)
        }
      }
    }
  }
}
