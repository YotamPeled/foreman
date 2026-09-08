import QtQuick
import Quickshell.Io
import "Ledger.js" as Ledger

// One front's six ledgers, watched. Nothing here interprets a row: the model
// owns the meaning, this owns the reading. Every view is blockLoading so a
// re-read is finished by the time reload() returns and the model can fold in
// one pass instead of chasing six separate loaded signals.
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

  function reload() {
    frontFile.reload()
    tasksFile.reload()
    jobsFile.reload()
    evidenceFile.reload()
    findingsFile.reload()
    measurementsFile.reload()
    feed.refold()
  }

  function refold() {
    var rows = Ledger.ledger(frontFile.text())
    feed.front = rows.length > 0 ? rows[rows.length - 1] : ({})
    feed.tasks = Ledger.ledger(tasksFile.text())
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
    onFileChanged: feed.refold()
    onLoaded: feed.refold()
  }
  property FileView tasksFile: FileView {
    path: feed.dir + "/tasks.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: feed.refold()
    onLoaded: feed.refold()
  }
  property FileView jobsFile: FileView {
    path: feed.dir + "/jobs.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: feed.refold()
    onLoaded: feed.refold()
  }
  property FileView evidenceFile: FileView {
    path: feed.dir + "/evidence.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: feed.refold()
    onLoaded: feed.refold()
  }
  property FileView findingsFile: FileView {
    path: feed.dir + "/findings.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: feed.refold()
    onLoaded: feed.refold()
  }
  property FileView measurementsFile: FileView {
    path: feed.dir + "/measurements.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: feed.refold()
    onLoaded: feed.refold()
  }

  Component.onCompleted: feed.refold()
}
