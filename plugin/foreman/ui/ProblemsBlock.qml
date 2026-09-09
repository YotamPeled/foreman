import QtQuick
import QtQuick.Layouts
import qs.Commons
import ".." as Foreman

// The mock's PROBLEMS section: one red card per open anomaly. The anomaly
// sentence is the title; where the anomaly carries an action there is an
// action pill with its static key chip, and the kind, subject and wait sit
// beneath in the small muted style. Empty renders the foreman status
// voice: none.
Item {
  id: root

  readonly property var rows: Foreman.Model.problems.rows

  // Two-letter hints from Keys, assigned on appearance, one per anomaly
  // that carries an action. The mock's single letters show where the
  // chips sit, not the scheme.

  function ageText(s) {
    if (!(s >= 0)) return "waited unknown"
    var m = Math.floor(s / 60)
    if (m < 1) return "waited " + Math.floor(s) + "s"
    if (m < 60) return "waited " + m + " min"
    return "waited " + Math.floor(m / 60) + "h " + (m % 60) + "m"
  }

  // The model carries the full CLI action (e.g. the mock intruder's
  // "foreman kill ses-wcorp002 --reason intruder-pid-41180"); the panel
  // shows the mock's short verb.
  function actionLabel(action) {
    var words = String(action || "").replace(/^\s*foreman\s+/i, "").split(/\s+/)
    return words.length > 0 ? words[0] : ""
  }

  implicitHeight: col.implicitHeight
  height: implicitHeight

  Column {
    id: col
    anchors {
      left: parent.left
      right: parent.right
      top: parent.top
    }
    spacing: Style.space(10)

    SectionHeading {
      title: "Problems"
      width: parent.width
    }

    Repeater {
      model: root.rows
      delegate: Card {
        tone: "red"
        width: col.width

        RowLayout {
          width: parent.width
          spacing: Style.space(20)

          Text {
            objectName: "title"
            Layout.fillWidth: true
            text: modelData.detail
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.heading
            wrapMode: Text.Wrap
          }

          Row {
            visible: modelData.action !== ""
            spacing: Style.space(6)
            KeyChip {
              key: Foreman.Keys.hintFor("problem:" + modelData.kind + ":" + modelData.subject)
              opacity: Foreman.Keys.dimFor(key) ? 0.35 : 1.0
            }
            Pill {
              label: root.actionLabel(modelData.action)
              tone: root.actionLabel(modelData.action) === "kill" ? "red" : "plain"
            }
          }
        }

        Text {
          width: parent.width
          text: (modelData.kind || "") + " · " + (modelData.subject || "") + " · " + root.ageText(modelData.sinceS)
          color: Color.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }
      }
    }

    Text {
      objectName: "empty"
      visible: root.rows.length === 0
      width: parent.width
      text: "none."
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.Wrap
    }
  }
}
