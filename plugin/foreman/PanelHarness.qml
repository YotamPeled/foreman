import Quickshell
import QtQuick
import "." as Foreman
import "ui" as Ui

// The panel with no window on it: `bin/foreman-verify panel --blocks` runs
// this under QT_QPA_PLATFORM=offscreen with FOREMAN_STATE pointing at a
// fixture. It instantiates the same header row and block files Panel.qml
// loads, walks what each one actually shows, and prints one RENDER line
// per slot:
//
//   RENDER <slot> <ready|missing|error> <cards> {"titles": [...], "empty": [...]}
//
// cards counts the Card components on screen (via isForemanCard), titles
// are the on-screen title Texts (objectName "title"), and empty is the
// on-screen empty-state voice (objectName "empty"). Block files
// that have not landed yet report missing, so this stays meaningful as
// the other layout jobs arrive: their files are picked up with no change
// here, provided their cards use ui/Card and mark titles the same way.
//
// Like Harness.qml this settles: it prints once the model counts and the
// rendered lines have stopped moving, and gives up printing whatever it
// has at the deadline.
ShellRoot {
  id: harness

  property string signature: ""
  property int stable: 0
  property int ticks: 0

  readonly property int settleTicks: 4     // 4 x 250ms unchanged
  readonly property int deadlineTicks: 40  // 10s, then print regardless

  // The slots in Panel.qml's order: the model block name and the file the
  // panel's Loader points at.
  readonly property var slots: [
    { block: "needsYou", file: "ui/NeedsYouBlock.qml" },
    { block: "problems", file: "ui/ProblemsBlock.qml" },
    { block: "working", file: "ui/WorkingBlock.qml" },
    { block: "jobQueue", file: "ui/JobQueueBlock.qml" },
    { block: "frontQueue", file: "ui/FrontQueueBlock.qml" },
    { block: "mergeQueue", file: "ui/MergeQueueBlock.qml" },
    { block: "capacity", file: "ui/CapacityBlock.qml" }
  ]

  property var holders: []

  function counts() {
    var names = Foreman.Model.blockNames
    var out = []
    for (var i = 0; i < names.length; i++)
      out.push(names[i] + " " + Foreman.Model.block(names[i]).count)
    return out
  }

  function isMissingError(err) {
    return /not found|does not exist|no such file|cannot open/i.test(String(err || ""))
  }

  function loadAll() {
    var next = []
    for (var i = 0; i < harness.slots.length; i++) {
      var comp = Qt.createComponent(harness.slots[i].file)
      if (comp.status === Component.Ready) {
        next.push({ status: "ready", item: comp.createObject(stage, { width: stage.width }) })
      } else if (isMissingError(comp.errorString())) {
        next.push({ status: "missing", item: null })
      } else {
        console.log("SLOT-ERROR " + harness.slots[i].block + " " + comp.errorString())
        next.push({ status: "error", item: null })
      }
    }
    harness.holders = next
  }

  function walk(item, acc) {
    if (!item) return
    if (item.isForemanCard === true && item.visible) acc.cards += 1
    if (item.visible && typeof item.text === "string") {
      if (item.objectName === "title") acc.titles.push(item.text)
      else if (item.objectName === "empty") acc.empty.push(item.text)
    }
    var kids = item.children
    if (kids)
      for (var i = 0; i < kids.length; i++) walk(kids[i], acc)
  }

  function headerTexts() {
    var out = []
    // PanelHeader's texts are grandchildren (inside its RowLayout).
    var stack = [panelHeader]
    while (stack.length > 0) {
      var node = stack.pop()
      if (node.objectName === "htext" && node.visible && typeof node.text === "string")
        out.push(node.text)
      var kids2 = node.children
      if (kids2)
        for (var i = kids2.length - 1; i >= 0; i--) stack.push(kids2[i])
    }
    return out
  }

  function renders() {
    var out = []
    out.push("header ready 0 " + JSON.stringify({ titles: headerTexts(), empty: [] }))
    for (var i = 0; i < harness.slots.length; i++) {
      var holder = harness.holders[i]
      if (!holder || holder.status !== "ready" || !holder.item) {
        out.push(harness.slots[i].block + " " + (holder ? holder.status : "missing")
                 + " 0 " + JSON.stringify({ titles: [], empty: [] }))
        continue
      }
      var acc = { cards: 0, titles: [], empty: [] }
      harness.walk(holder.item, acc)
      out.push(harness.slots[i].block + " ready " + acc.cards + " "
               + JSON.stringify({ titles: acc.titles, empty: acc.empty }))
    }
    return out
  }

  function report(settled) {
    var lines = harness.counts()
    for (var i = 0; i < lines.length; i++) console.log("BLOCK " + lines[i])
    console.log("NONEMPTY " + Foreman.Model.nonEmptyBlocks())
    var rendered = harness.renders()
    for (var r = 0; r < rendered.length; r++) console.log("RENDER " + rendered[r])
    console.log("STATEDIR " + Foreman.Model.stateDir)
    console.log(settled ? "HARNESS-DONE settled" : "HARNESS-DONE deadline")
    Qt.exit(0)
  }

  Component.onCompleted: harness.loadAll()

  // Never shown; the fixed width only gives wrapping texts a viewport.
  Item {
    id: stage
    width: 1100
    height: 10

    Ui.PanelHeader {
      id: panelHeader
      width: stage.width
    }
  }

  Timer {
    interval: 250
    running: true
    repeat: true
    onTriggered: {
      harness.ticks += 1
      var current = harness.counts().join(",") + "\n" + harness.renders().join("\n")
      if (current === harness.signature) harness.stable += 1
      else { harness.signature = current; harness.stable = 0 }
      if (harness.ticks >= harness.settleTicks && harness.stable >= harness.settleTicks) {
        running = false
        harness.report(true)
      } else if (harness.ticks >= harness.deadlineTicks) {
        running = false
        harness.report(false)
      }
    }
  }
}
