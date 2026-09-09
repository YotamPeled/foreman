pragma Singleton
pragma ComponentBehavior: Bound

import QtQml
import QtQuick
import Quickshell
import Quickshell.Io

// The whole panel's data layer: one file, read once per change.
//
// This used to read the state directory itself — a watched FileView per
// top-level ledger, six per front, four per session, two directory scans —
// and every one of those loads rebuilt all eight blocks over all fronts and
// all sessions. Against the owner's own state that was about 350 reloads and
// 350 full rebuilds per poll tick, and it ran on the shell's main thread:
// measured, 11.2 seconds of block out of every 11.3. The ledgers are not
// big and the fold is not slow. The fan-out was the defect, so all of it is
// gone.
//
// The runtime folds now. `foreman panel-feed` writes panel.json in the state
// directory — exactly the facts these eight blocks render, produced by the
// same gathering `foreman status` reads — the collector rewrites it on every
// tick, and every verb rewrites it on its way out. This reads that one file
// and parses nothing else: no ledger, no directory scan, no fold.
//
// The state directory is $FOREMAN_STATE when set, ~/.local/state/foreman
// otherwise. That single switch is how a fixture is fed to this model.
//
// Feeding is one watcher on panel.json plus the 2 second poll as the floor
// under it, for the two things a watch can miss: the file created after the
// panel started, and an atomic replace swapping the inode out from under the
// watch. A tick where nothing changed re-reads one small file and stops
// there: the bytes match what was already folded, so nothing is parsed and
// no block is rebuilt.
QtObject {
  id: root

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateDir: {
    var override = Quickshell.env("FOREMAN_STATE")
    if (override && override.length > 0) return override
    return root.home + "/.local/state/foreman"
  }

  // The collector's own clock, so every age on the panel is measured from
  // the same tick rather than from when a binding happened to re-evaluate.
  readonly property string now: root.feed.now || ""

  signal changed()

  // ---- the eight blocks of DESIGN section 13 -------------------------------
  // Read, never computed. The shapes are the ones the ui/ files bind to.

  property var header: ({ count: 0, rows: [], registered: 0, observed: 0,
                          frozen: false, collectorAgeS: null, collectorAlive: false })
  property var needsYou: ({ count: 0, rows: [] })
  property var problems: ({ count: 0, rows: [] })
  property var working: ({ count: 0, rows: [] })
  property var jobQueue: ({ count: 0, rows: [] })
  property var frontQueue: ({ count: 0, rows: [] })
  property var mergeQueue: ({ count: 0, rows: [], merging: [], waiting: [], landed: [] })
  property var capacity: ({ count: 0, rows: [] })

  readonly property var blockNames: ["header", "needsYou", "problems", "working",
                                     "jobQueue", "frontQueue", "mergeQueue", "capacity"]

  function block(name) {
    switch (name) {
      case "header": return root.header
      case "needsYou": return root.needsYou
      case "problems": return root.problems
      case "working": return root.working
      case "jobQueue": return root.jobQueue
      case "frontQueue": return root.frontQueue
      case "mergeQueue": return root.mergeQueue
      case "capacity": return root.capacity
    }
    return ({ count: 0, rows: [] })
  }

  // How many of the eight came back with something in them.
  function nonEmptyBlocks() {
    var n = 0
    for (var i = 0; i < root.blockNames.length; i++)
      if (root.block(root.blockNames[i]).count > 0) n += 1
    return n
  }

  // ---- what the components join by session or role -------------------------

  // Every front the summary names, running or queued or done. Working draws
  // the running ones; this is what a reader that wants all of them asks.
  property var fronts: []

  // session id -> { front, role, state }: the two joins the components make
  // from a session id, and nothing else of the roster reaches the panel.
  property var roster: ({})

  // The role-to-pool lines, so a job that carries a role can be counted
  // against the pool it actually waits on.
  property var slots: []

  property bool frozen: false

  // Which pool a role waits on. A job carries its role (muse, astra) but
  // waits on a pool (muse, codex): the slots ledger carries the mapping,
  // later lines winning. A role with no slot line yet waits on the pool of
  // the same name.
  function poolFor(role) {
    var pool = ""
    for (var i = 0; i < root.slots.length; i++) {
      var line = root.slots[i]
      if (line && line.role === role && typeof line.pool === "string" && line.pool)
        pool = line.pool
    }
    return pool || role
  }

  function supervisorFor(front) {
    for (var i = 0; i < root.fronts.length; i++)
      if (root.fronts[i].name === front) return root.fronts[i].supervisor || ""
    return ""
  }

  // ---- reading -------------------------------------------------------------

  // The whole summary, as parsed. Empty until the first load, so a panel
  // opened before the runtime has ever folded draws empty blocks rather
  // than erroring.
  property var feed: ({})

  // The bytes already folded. Every load compares against these and returns
  // without parsing when they match, so a tick where nothing moved costs one
  // read of one small file and no rebuild at all. (Quickshell's FileView
  // exposes no stat, so this compares the bytes rather than the modification
  // time; the read is the cheap half and the fold is the half that mattered.)
  property string seen: ""

  function apply(data) {
    if (!data || typeof data !== "object") return
    root.feed = data
    root.header = data.header || root.header
    root.needsYou = data.needsYou || ({ count: 0, rows: [] })
    root.problems = data.problems || ({ count: 0, rows: [] })
    root.working = data.working || ({ count: 0, rows: [] })
    root.jobQueue = data.jobQueue || ({ count: 0, rows: [] })
    root.frontQueue = data.frontQueue || ({ count: 0, rows: [] })
    root.mergeQueue = data.mergeQueue
      || ({ count: 0, rows: [], merging: [], waiting: [], landed: [] })
    root.capacity = data.capacity || ({ count: 0, rows: [] })
    root.fronts = data.fronts || []
    root.roster = data.roster || ({})
    root.slots = data.slots || []
    root.frozen = data.frozen === true
    root.freshenCollector()
    root.changed()
  }

  // The one fact the file cannot carry frozen: how old the collector's last
  // tick is. Every other age is measured from that tick, so it belongs to
  // the file; this one is measured against the wall clock, and freezing it
  // would leave a dead collector reading "alive" forever. The file carries
  // the tick's timestamp and this ages it — one Date.parse, on the same
  // 2 second beat the poll already runs.
  function freshenCollector() {
    var head = root.header
    if (!head) return
    var age = null
    if (typeof head.collectorAt === "string" && head.collectorAt !== "") {
      var at = Date.parse(head.collectorAt)
      if (!isNaN(at)) age = (Date.now() - at) / 1000
    }
    if (head.collectorAgeS === age) return
    var next = ({})
    for (var key in head) next[key] = head[key]
    next.collectorAgeS = age
    next.collectorAlive = age !== null && age < 120
    var rows = (head.rows || []).slice()
    for (var i = 0; i < rows.length; i++)
      if (rows[i] && rows[i].label === "collector")
        rows[i] = { label: "collector", value: (age === null) ? "never ticked" : age }
    next.rows = rows
    root.header = next
  }

  property FileView panelFile: FileView {
    path: root.stateDir + "/panel.json"
    watchChanges: true
    blockLoading: true
    printErrors: false
    // A watch notification means the bytes changed, not that text() did:
    // the text is cached until the load delivers. So the change only asks
    // for the re-read and the load does the parse.
    onFileChanged: panelFile.reload()
    onLoaded: {
      var text = panelFile.text()
      if (text === root.seen) return
      root.seen = text
      try {
        root.apply(JSON.parse(text))
      } catch (e) {
        // A summary caught mid-replace, or one a future runtime writes in a
        // shape this panel cannot read. Keep the last good blocks on screen.
      }
    }
  }

  // The floor under the watcher: a file created after start, and an atomic
  // replace that swaps the inode, both land here rather than being missed.
  // Nothing changed costs one stat.
  property Timer poll: Timer {
    interval: 2000
    running: true
    repeat: true
    onTriggered: {
      root.freshenCollector()
      panelFile.reload()
    }
  }

  Component.onCompleted: panelFile.reload()
}
