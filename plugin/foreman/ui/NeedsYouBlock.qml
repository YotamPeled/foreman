import QtQuick
import QtQuick.Layouts
import qs.Commons
import ".." as Foreman

// The mock's NEEDS YOU section: one yellow card per open inbox item. Each
// card shows the question, static approve/decline pills with their key
// chips, the recommendation with the recommended answer coloured, and the
// kind and wait in the small muted style. Empty renders the foreman status
// voice: nothing needs you.
Item {
  id: root

  readonly property var rows: Foreman.Model.needsYou.rows

  // Static key letters until the keys job wires real bindings: the mock's
  // own letters by card position, nothing once they run out.
  readonly property var approveKeys: ["a", "d"]
  readonly property var declineKeys: ["s", "g"]

  function ageText(s) {
    if (!(s >= 0)) return "waited unknown"
    var m = Math.floor(s / 60)
    if (m < 1) return "waited " + Math.floor(s) + "s"
    if (m < 60) return "waited " + m + " min"
    return "waited " + Math.floor(m / 60) + "h " + (m % 60) + "m"
  }

  function recWord(rec) {
    var m = String(rec || "").match(/^\s*([A-Za-z]+)/)
    return m ? m[1] : ""
  }

  function recRest(rec) {
    var m = String(rec || "").match(/^\s*[A-Za-z]+([\s\S]*)$/)
    return m ? m[1] : String(rec || "")
  }

  function recKnown(word, options) {
    var lower = String(word).toLowerCase()
    for (var i = 0; i < (options || []).length; i++)
      if (String(options[i]).toLowerCase() === lower) return true
    return false
  }

  function recColor(word) {
    var lower = String(word).toLowerCase()
    if (lower === "yes") return Color.accent
    if (lower === "no") return Color.urgent
    return Color.menu.text
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
      title: "Needs you"
      width: parent.width
    }

    Repeater {
      model: root.rows
      delegate: Card {
        tone: "yellow"
        width: col.width

        RowLayout {
          width: parent.width
          spacing: Style.space(20)

          Text {
            objectName: "title"
            Layout.fillWidth: true
            text: modelData.question
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.heading
            wrapMode: Text.Wrap
          }

          Row {
            spacing: Style.space(6)
            KeyChip { key: index < root.approveKeys.length ? root.approveKeys[index] : "" }
            Pill { label: "approve"; tone: "green" }
            KeyChip { key: index < root.declineKeys.length ? root.declineKeys[index] : "" }
            Pill { label: "decline"; tone: "plain" }
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(4)

          Text {
            text: "Foreman recommends"
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }

          Text {
            text: root.recWord(modelData.recommendation)
            visible: text !== ""
            color: root.recKnown(text, modelData.options) ? root.recColor(text) : Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            font.bold: true
          }

          Text {
            text: root.recRest(modelData.recommendation)
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            width: Math.max(0, parent.width - x)
            wrapMode: Text.Wrap
          }
        }

        Text {
          width: parent.width
          text: (modelData.kind || "") + " · " + root.ageText(modelData.waitedS)
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
      text: "nothing needs you."
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.Wrap
    }
  }
}
