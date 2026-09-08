import QtQuick
import Quickshell.Io
import "Ledger.js" as Ledger

// One front's six ledgers, watched. Nothing here interprets a row: the model
// owns the meaning, this owns the reading.
//
// A watch notification means the bytes changed, not that FileView.text()
// did: the text is cached, and reload() still returns the old text until
// onLoaded delivers the new data. So every onFileChanged only asks for the
// re-read, and every onLoaded does the fold. Folding in onFileChanged would
// rebuild the model on the stale text; folding anywhere but onLoaded would
// rebuild across a mix of old and new files.
QtObject {
  id: feed

  // Front directory name, and the directory itself. Both are given; neither
  // is derived here, because only the model knows where the state dir is.
  required property string name
  required property string dir

  signal updated()

  property var front: ({})
  property var tasks: []
  property var jobs: []
  property var evidence: []
  property var findings: []
  property var monitors: []

  // The task lines unfolded: successive lines for one id carry its
  // units_done over time, which is what the model derives the rate from.
  property var taskHistory: []

  function reload() {
    frontFile.reload()
    tasksFile.reload()
    jobsFile.reload()
    evidenceFile.reload()
    findingsFile.reload()
    measurementsFile.reload()
  }

  function refold() {
    var rows = Ledger.ledger(frontFile.text())
    feed.front = rows.length > 0 ? rows[rows.length - 1] : ({})
    feed.tasks = Ledger.ledger(tasksFile.text())
    feed.taskHistory = Ledger.records(tasksFile.text())
    feed.jobs = Ledger.ledger(jobsFile.text())
    feed.evidence = Ledger.records(evidenceFile.text())
    feed.findings = Ledger.records(findingsFile.text())
    // A measurement carries no id; the monitor's question is its identity and
    // the last line is the latest reading.
    feed.monitors = Ledger.foldBy(Ledger.records(measurementsFile.text()),
                                  function (row) { return row.monitor || "" })
    feed.updated()
  }

  property FileView frontFile: FileView {
    path: feed.dir + "/front.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: frontFile.reload()
    onLoaded: feed.refold()
  }
  property FileView tasksFile: FileView {
    path: feed.dir + "/tasks.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: tasksFile.reload()
    onLoaded: feed.refold()
  }
  property FileView jobsFile: FileView {
    path: feed.dir + "/jobs.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: jobsFile.reload()
    onLoaded: feed.refold()
  }
  property FileView evidenceFile: FileView {
    path: feed.dir + "/evidence.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: evidenceFile.reload()
    onLoaded: feed.refold()
  }
  property FileView findingsFile: FileView {
    path: feed.dir + "/findings.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: findingsFile.reload()
    onLoaded: feed.refold()
  }
  property FileView measurementsFile: FileView {
    path: feed.dir + "/measurements.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: measurementsFile.reload()
    onLoaded: feed.refold()
  }

  Component.onCompleted: feed.refold()
}
