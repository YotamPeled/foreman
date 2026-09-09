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

  // Two-letter hints from Keys, assigned on appearance: each open item's
  // approve then decline. The mock's single letters show where the chips
  // sit, not the scheme.

  function ageText(s) {
    s = Foreman.Model.liveAge(s)
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
            KeyChip {
              key: Foreman.Keys.hintFor("answer:" + modelData.id + ":approve")
              opacity: Foreman.Keys.dimFor(key) ? 0.35 : 1.0
            }
            Pill { label: "approve"; tone: "green" }
            KeyChip {
              key: Foreman.Keys.hintFor("answer:" + modelData.id + ":decline")
              opacity: Foreman.Keys.dimFor(key) ? 0.35 : 1.0
            }
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
          objectName: "age"
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
