import QtQuick
import QtQuick.Layouts
import qs.Commons
import ".." as Foreman

// The mock's header row: foreman in accent, then the time, sessions,
// frozen and collector state, spread across the width. All four values
// come from the header block. Shared by Panel.qml and the --blocks
// harness so the assertion reads the same row the panel shows.
//
// panel --blocks collects every Text with objectName "htext" here.
Item {
  id: root

  readonly property string timeText: {
    var d = new Date(Foreman.Model.now)
    if (isNaN(d.getTime())) return ""
    var days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    function pad(n) { return (n < 10 ? "0" : "") + n }
    // UTC: the model carries an instant with no zone, so the collector
    // clock is what the panel shows, wherever it is opened.
    return days[d.getUTCDay()] + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes())
  }
  readonly property string sessionsText: {
    var h = Foreman.Model.header
    if (h.observed === h.registered) return h.registered + " sessions, all registered"
    return h.registered + " registered, " + h.observed + " observed"
  }
  readonly property string frozenText: Foreman.Model.header.frozen ? "frozen" : "not frozen"
  readonly property string collectorText: {
    var h = Foreman.Model.header
    if (h.collectorAlive) return "collector alive"
    if (h.collectorAgeS === null || h.collectorAgeS === undefined) return "collector never ticked"
    return "collector stale"
  }

  implicitHeight: row.implicitHeight
  height: implicitHeight

  RowLayout {
    id: row
    width: parent.width
    spacing: Style.space(20)

    Text {
      objectName: "htext"
      text: "foreman"
      color: Color.accent
      font.family: Style.font.family
      font.pixelSize: Style.font.heading
    }

    Item { Layout.fillWidth: true }

    Text {
      objectName: "htext"
      text: root.timeText
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    Item { Layout.fillWidth: true }

    Text {
      objectName: "htext"
      text: root.sessionsText
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    Item { Layout.fillWidth: true }

    Text {
      objectName: "htext"
      text: root.frozenText
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    Item { Layout.fillWidth: true }

    Text {
      objectName: "htext"
      text: root.collectorText
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }
  }
}
