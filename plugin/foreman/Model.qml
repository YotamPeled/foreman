pragma Singleton
pragma ComponentBehavior: Bound

import QtQml
import QtQuick
import Quickshell
import Quickshell.Io
import Qt.labs.folderlistmodel
import "Ledger.js" as Ledger

// The whole panel's data layer. Every read of the Foreman state directory
// happens here and nowhere else, and it is a read of files: the `foreman`
// command is never run, because the files are the interface and the panel
// must keep showing the swarm even when the CLI cannot run.
//
// The state directory is $FOREMAN_STATE when set, ~/.local/state/foreman
// otherwise. That single switch is how a fixture is fed to this model.
//
// Feeding is a file watcher per file plus one 2 second re-read. The watcher
// misses two things that happen constantly here: a ledger created after the
// panel started (a front's first task), and an atomic replace, which swaps
// the inode out from under an inotify watch. The poll is the floor under
// both, not the primary path.
QtObject {
  id: root

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateDir: {
    var override = Quickshell.env("FOREMAN_STATE")
    if (override && override.length > 0) return override
    return root.home + "/.local/state/foreman"
  }
  readonly property string frontsDir: root.stateDir + "/fronts"

  // The collector's own clock where it has one, so every age on the panel is
  // measured from the same tick rather than from when a binding happened to
  // re-evaluate.
  readonly property string now: (root.observed && root.observed.at) ? root.observed.at : ""

  signal changed()

  // ---- the eight blocks of DESIGN section 13 -------------------------------

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

  // ---- raw state ----------------------------------------------------------

  property var roster: ({})            // session id -> record
  property var observed: null          // the collector's derived snapshot
  property var collector: null
  property var inbox: []               // folded, every item
  property var merges: []              // folded, every row
  property var anomalies: []           // folded by (kind, subject)
  property var slots: []
  property bool frozen: false

  // name -> FrontFeed. Rebuilt only when the set of front directories
  // changes; a front's own six watchers survive a rescan.
  property var feeds: ({})
  property var frontNames: []

  // ---- reading ------------------------------------------------------------

  function reload() {
    rosterFile.reload()
    observedFile.reload()
    collectorFile.reload()
    inboxFile.reload()
    mergesFile.reload()
    anomaliesFile.reload()
    slotsFile.reload()
    frozenFile.reload()
    frontsFolder.folder = ""
    frontsFolder.folder = "file://" + root.frontsDir
    for (var name in root.feeds) root.feeds[name].reload()
    root.refold()
  }

  function refold() {
    var rosterSnapshot = Ledger.snapshot(rosterFile.text(), ({}))
    root.roster = (rosterSnapshot && rosterSnapshot.sessions) ? rosterSnapshot.sessions : ({})
    root.observed = Ledger.snapshot(observedFile.text(), null)
    root.collector = Ledger.snapshot(collectorFile.text(), null)
    // inbox.jsonl and merges.jsonl have no writer on some machines yet.
    // Absent is an empty block, never an error.
    root.inbox = Ledger.ledger(inboxFile.text())
    root.merges = Ledger.ledger(mergesFile.text())
    root.anomalies = Ledger.foldAnomalies(Ledger.records(anomaliesFile.text()))
    root.slots = Ledger.records(slotsFile.text())
    root.frozen = frozenFile.loaded === true
    root.rebuild()
  }

  // Fold every block out of the raw state in one pass, so the panel never
  // shows a header from this tick beside a queue from the last one.
  function rebuild() {
    root.header = root.buildHeader()
    root.needsYou = root.buildNeedsYou()
    root.problems = root.buildProblems()
    var fronts = root.buildFronts()
    root.working = { count: fronts.working.length, rows: fronts.working }
    root.frontQueue = { count: fronts.queued.length, rows: fronts.queued }
    root.jobQueue = root.buildJobQueue(fronts.all)
    root.mergeQueue = root.buildMergeQueue(fronts.taskTitles)
    root.capacity = root.buildCapacity(root.jobQueue.rows)
    root.changed()
  }

  // ---- the blocks ---------------------------------------------------------

  function buildHeader() {
    var swarm = (root.observed && root.observed.swarm) ? root.observed.swarm : ({})
    var registered = (typeof swarm.sessions_registered === "number")
      ? swarm.sessions_registered : Object.keys(root.roster).length
    var seen = (typeof swarm.sessions_observed === "number") ? swarm.sessions_observed : 0
    var age = root.observed ? Ledger.seconds(root.observed.at, "") : null
    return {
      count: registered,
      registered: registered,
      observed: seen,
      frozen: root.frozen,
      collectorAgeS: age,
      collectorAlive: age !== null && age < 120,
      inboxDepth: (typeof swarm.inbox_depth === "number") ? swarm.inbox_depth : 0,
      openAnomalies: (typeof swarm.open_anomalies === "number") ? swarm.open_anomalies : 0,
      rows: [
        { label: "sessions", value: registered + " registered, " + seen + " observed" },
        { label: "frozen", value: root.frozen ? "frozen" : "not frozen" },
        { label: "collector", value: age === null ? "never ticked" : age }
      ]
    }
  }

  function buildNeedsYou() {
    var open = Ledger.unset(root.inbox, "answered_at")
    var rows = []
    var sorted = Ledger.sortByKey(open, "asked_at")
    for (var i = 0; i < sorted.length; i++) {
      var item = sorted[i]
      rows.push({
        id: item.id || "",
        question: item.question || "",
        recommendation: item.recommendation || "",
        kind: item.kind || "",
        from: item["from"] || "",
        options: item.options || [],
        waitedS: Ledger.seconds(item.asked_at, root.now)
      })
    }
    return { count: rows.length, rows: rows }
  }

  function buildProblems() {
    var open = Ledger.unset(root.anomalies, "resolved_at")
    var rows = []
    for (var i = 0; i < open.length; i++) {
      var row = open[i]
      var session = root.roster[row.subject]
      rows.push({
        kind: row.kind || "",
        subject: row.subject || "",
        detail: row.detail || "",
        front: (session && session.front) ? session.front : "",
        action: row.action || "",
        sinceS: Ledger.seconds(row.since, root.now)
      })
    }
    return { count: rows.length, rows: rows }
  }

  // Working and the front queue come out of the same walk: a front is either
  // running (its supervisor is on it) or it is waiting to start.
  function buildFronts() {
    var workingRows = []
    var queuedRows = []
    var all = []
    var taskTitles = ({})
    var observedFronts = (root.observed && root.observed.fronts) ? root.observed.fronts : ({})
    var observedJobs = (root.observed && root.observed.jobs) ? root.observed.jobs : ({})

    for (var f = 0; f < root.frontNames.length; f++) {
      var name = root.frontNames[f]
      var feed = root.feeds[name]
      if (!feed) continue
      var record = feed.front || ({})
      var derived = observedFronts[name] || ({})
      var tasks = feed.tasks
      var jobs = feed.jobs

      var landed = 0, built = 0
      var remaining = []
      var taskRows = []
      for (var t = 0; t < tasks.length; t++) {
        var task = tasks[t]
        if (task.id) taskTitles[task.id] = task.title || task.id
        if (task.state === "landed") landed += 1
        else if (task.state === "built") built += 1
        else remaining.push(task.title || task.id || "")
        taskRows.push({
          id: task.id || "",
          title: task.title || "",
          state: task.state || "",
          unitsDone: task.units_done || 0,
          unitsTotal: task.units_total || 0,
          after: task.after || [],
          jobs: root.jobRows(jobs, task.id, observedJobs)
        })
      }

      var supervisor = root.supervisorFor(name)
      var row = {
        name: name,
        want: record.want || "",
        state: record.state || "",
        landOn: record.land_on || "",
        supervisor: supervisor,
        doingNow: derived.doing_now || "",
        doingAgeS: (typeof derived.doing_age_s === "number") ? derived.doing_age_s : null,
        ratePerHour: (typeof derived.rate_per_hour === "number") ? derived.rate_per_hour : null,
        projectedFinish: derived.projected_finish || "",
        landed: landed,
        built: built,
        total: tasks.length,
        remaining: remaining,
        blockedOnOwner: root.inboxFrom(supervisor),
        tasks: taskRows,
        monitors: root.monitorRows(feed.monitors)
      }
      all.push(row)
      if (record.state === "queued")
        queuedRows.push({
          name: name,
          want: record.want || "",
          order: (typeof record.order === "number") ? record.order : 0,
          prefer: (typeof record.prefer === "number") ? record.prefer : 0,
          after: record.after || [],
          waitsFor: root.waitsFor(record)
        })
      else if (record.state !== "done")
        workingRows.push(row)
    }

    queuedRows.sort(function (a, b) {
      if (b.prefer !== a.prefer) return b.prefer - a.prefer
      return a.order - b.order
    })
    return { working: workingRows, queued: queuedRows, all: all, taskTitles: taskTitles }
  }

  function waitsFor(record) {
    var after = record.after || []
    if (after.length > 0) return "after " + after.join(", ")
    return "capacity"
  }

  function jobRows(jobs, taskId, observedJobs) {
    var rows = []
    for (var j = 0; j < jobs.length; j++) {
      var job = jobs[j]
      if (job.task !== taskId) continue
      rows.push(root.jobRow(job, observedJobs))
    }
    return rows
  }

  function jobRow(job, observedJobs) {
    var seen = observedJobs[job.id] || ({})
    var session = root.roster[job.session] || ({})
    return {
      id: job.id || "",
      task: job.task || "",
      kind: job.kind || "",
      role: job.role || "",
      state: (session.state === "stalled") ? "stalled" : (job.state || ""),
      worktree: job.worktree || "",
      branch: job.branch || "",
      elapsedS: (typeof seen.elapsed_s === "number") ? seen.elapsed_s : null,
      timeoutS: (typeof seen.timeout_s === "number") ? seen.timeout_s : null,
      minutesSinceWrite: (typeof seen.minutes_since_write === "number")
        ? seen.minutes_since_write : null,
      waitedS: Ledger.seconds(job.queued_at || job.planned_at, root.now)
    }
  }

  function monitorRows(monitors) {
    var rows = []
    for (var i = 0; i < monitors.length; i++) {
      var row = monitors[i]
      rows.push({
        monitor: row.monitor || "",
        value: row.value,
        of: (row.of === undefined) ? null : row.of,
        trend: row.trend || "",
        status: row.status || "",
        measuredS: Ledger.seconds(row.at, root.now)
      })
    }
    return rows
  }

  function supervisorFor(front) {
    var backup = ""
    for (var sid in root.roster) {
      var record = root.roster[sid]
      if (!record || record.role !== "supervisor" || record.front !== front) continue
      if (record.state === "running") return sid
      if (backup === "") backup = sid
    }
    return backup
  }

  function inboxFrom(sessionId) {
    if (!sessionId) return []
    var out = []
    var open = Ledger.unset(root.inbox, "answered_at")
    for (var i = 0; i < open.length; i++)
      if (open[i]["from"] === sessionId) out.push(open[i].question || "")
    return out
  }

  // The queue is what a supervisor planned and cannot start yet, in the order
  // that supervisor wrote it, oldest wait first across fronts.
  function buildJobQueue(fronts) {
    var rows = []
    var observedJobs = (root.observed && root.observed.jobs) ? root.observed.jobs : ({})
    for (var f = 0; f < fronts.length; f++) {
      var front = fronts[f]
      var feed = root.feeds[front.name]
      if (!feed) continue
      var titles = ({})
      for (var t = 0; t < feed.tasks.length; t++)
        if (feed.tasks[t].id) titles[feed.tasks[t].id] = feed.tasks[t].title || ""
      for (var j = 0; j < feed.jobs.length; j++) {
        var job = feed.jobs[j]
        if (job.state !== "planned" && job.state !== "queued") continue
        var row = root.jobRow(job, observedJobs)
        row.front = front.name
        row.title = titles[job.task] || job.task || ""
        rows.push(row)
      }
    }
    rows.sort(function (a, b) {
      var x = (a.waitedS === null) ? -1 : a.waitedS
      var y = (b.waitedS === null) ? -1 : b.waitedS
      return y - x
    })
    return { count: rows.length, rows: rows }
  }

  function buildMergeQueue(taskTitles) {
    var rows = [], merging = [], waiting = [], landed = []
    for (var i = 0; i < root.merges.length; i++) {
      var record = root.merges[i]
      var titles = []
      var tasks = record.tasks || []
      for (var t = 0; t < tasks.length; t++) titles.push(taskTitles[tasks[t]] || tasks[t])
      var state = record.landed_at ? "landed"
        : (record.result === "merging" ? "merging" : "waiting")
      var row = {
        id: record.id || "",
        branch: record.branch || "",
        target: record.target || "",
        from: record["from"] || "",
        tasks: titles,
        reviews: record.review_refs || [],
        state: state,
        requestedS: Ledger.seconds(record.requested_at, root.now),
        landedS: record.landed_at ? Ledger.seconds(record.landed_at, root.now) : null
      }
      rows.push(row)
      if (state === "landed") landed.push(row)
      else if (state === "merging") merging.push(row)
      else waiting.push(row)
    }
    return { count: rows.length, rows: rows, merging: merging,
             waiting: waiting, landed: landed }
  }

  function buildCapacity(queuedJobs) {
    var pools = (root.observed && root.observed.pools) ? root.observed.pools : ({})
    var waitingBy = ({})
    for (var q = 0; q < queuedJobs.length; q++) {
      var role = queuedJobs[q].role || ""
      waitingBy[role] = (waitingBy[role] || 0) + 1
    }
    var names = Object.keys(pools).sort()
    var rows = []
    for (var i = 0; i < names.length; i++) {
      var entry = pools[names[i]] || ({})
      rows.push({
        pool: names[i],
        held: (typeof entry.held === "number") ? entry.held : 0,
        total: (typeof entry.total === "number") ? entry.total : null,
        meter: (typeof entry.meter === "number") ? entry.meter : null,
        resetsAt: entry.resets_at || "",
        avgS: (typeof entry.avg_s === "number") ? entry.avg_s : null,
        p90S: (typeof entry.p90_s === "number") ? entry.p90_s : null,
        waiting: waitingBy[names[i]] || 0
      })
    }
    return { count: rows.length, rows: rows }
  }

  // ---- front discovery ----------------------------------------------------

  function syncFronts() {
    var names = []
    for (var i = 0; i < frontsFolder.count; i++) {
      var name = frontsFolder.get(i, "fileName")
      if (typeof name === "string" && name.length > 0 && name[0] !== ".") names.push(name)
    }
    names.sort()

    var next = ({})
    var dirty = names.length !== root.frontNames.length
    for (var n = 0; n < names.length; n++) {
      var existing = root.feeds[names[n]]
      if (existing) {
        next[names[n]] = existing
      } else {
        dirty = true
        next[names[n]] = feedComponent.createObject(root, {
          name: names[n],
          dir: root.frontsDir + "/" + names[n]
        })
      }
    }
    for (var old in root.feeds)
      if (!(old in next)) { dirty = true; root.feeds[old].destroy() }

    root.feeds = next
    root.frontNames = names
    if (dirty) root.refold()
    else root.rebuild()
  }

  property Component feedComponent: Component {
    FrontFeed {
      onUpdated: root.rebuild()
    }
  }

  property FolderListModel frontsFolder: FolderListModel {
    folder: "file://" + root.frontsDir
    showDirs: true
    showFiles: false
    showDotAndDotDot: false
    sortField: FolderListModel.Name
    onCountChanged: root.syncFronts()
    onStatusChanged: if (status === FolderListModel.Ready) root.syncFronts()
  }

  // ---- the watched files at the state root --------------------------------

  property FileView rosterFile: FileView {
    path: root.stateDir + "/roster.json"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: root.refold()
    onLoaded: root.refold()
  }
  property FileView observedFile: FileView {
    path: root.stateDir + "/observed.json"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: root.refold()
    onLoaded: root.refold()
  }
  property FileView collectorFile: FileView {
    path: root.stateDir + "/collector.json"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: root.refold()
    onLoaded: root.refold()
  }
  property FileView inboxFile: FileView {
    path: root.stateDir + "/inbox.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: root.refold()
    onLoaded: root.refold()
  }
  property FileView mergesFile: FileView {
    path: root.stateDir + "/merges.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: root.refold()
    onLoaded: root.refold()
  }
  property FileView anomaliesFile: FileView {
    path: root.stateDir + "/anomalies.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: root.refold()
    onLoaded: root.refold()
  }
  property FileView slotsFile: FileView {
    path: root.stateDir + "/slots.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: root.refold()
    onLoaded: root.refold()
  }
  // Presence is the whole signal: a frozen swarm has this file, a live one
  // does not.
  property FileView frozenFile: FileView {
    path: root.stateDir + "/frozen"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: root.refold()
    onLoaded: root.refold()
  }

  // The floor under the watchers: a file created after start, and an atomic
  // replace that swaps the inode, both land here rather than being missed.
  property Timer poll: Timer {
    interval: 2000
    running: true
    repeat: true
    onTriggered: root.reload()
  }

  Component.onCompleted: root.refold()
}
