import QtQuick
import Quickshell.Io
import "Ledger.js" as Ledger

// One headless session's liveness, watched: the last wake event out of
// sessions/<id>/events.jsonl, the turn count and last turn out of
// sessions/<id>/turns.jsonl, and the running turn's start out of
// sessions/<id>/turn.json. The model joins these to the front through
// the supervisor's roster row and renders the ages against its own
// clock, so this owns the reading and nothing else — the same split as
// CheckpointFeed. Same watch discipline as FrontFeed: onFileChanged only
// re-reads, onLoaded refolds.
QtObject {
  id: feed

  required property string sessionId
  required property string dir

  signal updated()

  property string wakeReason: ""
  property string wakeAt: ""
  property int turnCount: 0
  property string lastTurnAt: ""
  property string turnStartedAt: ""

  function reload() {
    eventsFile.reload()
    turnsFile.reload()
    turnFile.reload()
  }

  function latestByAt(rows) {
    var best = null
    for (var i = 0; i < rows.length; i++) {
      var at = rows[i].at
      if (typeof at !== "string" || at === "") continue
      if (best === null || at > best.at) best = rows[i]
    }
    return best
  }

  function refold() {
    var wake = feed.latestByAt(Ledger.records(eventsFile.text()))
    feed.wakeReason = (wake && typeof wake.reason === "string") ? wake.reason : ""
    feed.wakeAt = (wake && typeof wake.at === "string") ? wake.at : ""
    var turns = Ledger.records(turnsFile.text())
    feed.turnCount = turns.length
    var last = feed.latestByAt(turns)
    feed.lastTurnAt = (last && typeof last.at === "string") ? last.at : ""
    var marker = Ledger.snapshot(turnFile.text(), ({}))
    if (!marker || typeof marker !== "object") marker = ({})
    feed.turnStartedAt = (typeof marker.started_at === "string") ? marker.started_at : ""
    feed.updated()
  }

  property FileView eventsFile: FileView {
    path: feed.dir + "/events.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: eventsFile.reload()
    onLoaded: feed.refold()
  }
  property FileView turnsFile: FileView {
    path: feed.dir + "/turns.jsonl"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: turnsFile.reload()
    onLoaded: feed.refold()
  }
  property FileView turnFile: FileView {
    path: feed.dir + "/turn.json"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: turnFile.reload()
    onLoaded: feed.refold()
  }

  Component.onCompleted: feed.refold()
}
