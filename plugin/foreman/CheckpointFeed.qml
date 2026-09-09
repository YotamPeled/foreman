import QtQuick
import Quickshell.Io
import "Ledger.js" as Ledger

// One session's text status, watched. The collector never writes doing-now
// into observed.json: the supervisor's checkpoint.json holds `doing` and
// `next`, which is also where the text status reads them. Same watch
// discipline as FrontFeed: onFileChanged only re-reads, onLoaded refolds,
// because the cached text is stale until the load delivers.
QtObject {
  id: feed

  required property string sessionId
  required property string dir

  signal updated()

  property string doing: ""
  property string next: ""

  function reload() {
    checkpointFile.reload()
  }

  function refold() {
    var data = Ledger.snapshot(checkpointFile.text(), ({}))
    if (!data || typeof data !== "object") data = ({})
    feed.doing = (typeof data.doing === "string") ? data.doing : ""
    feed.next = (typeof data.next === "string") ? data.next : ""
    feed.updated()
  }

  property FileView checkpointFile: FileView {
    path: feed.dir + "/checkpoint.json"
    watchChanges: true
    blockLoading: true
    printErrors: false
    onFileChanged: checkpointFile.reload()
    onLoaded: feed.refold()
  }

  Component.onCompleted: feed.refold()
}
