import QtQuick
import QtQuick.Layouts
import qs.Commons
import ".." as Foreman

// FRONT QUEUE: the block the mock does not draw, in the mock's grammar. One
// card shaped exactly like the job queue's: per row the wait reason in the
// left column, then the front and what it is. No summary line.
Item {
  id: root

  readonly property var rows: Foreman.Model.frontQueue.rows
  readonly property color waitColor: Color.accent

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
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
      title: "Front queue"
      note: "waiting fronts, in preference order"
      width: parent.width
    }

    Card {
      tone: "plain"
      width: col.width
      visible: root.rows.length > 0

      Column {
        id: queueCol
        width: parent.width
        spacing: Style.space(3)

        Repeater {
          model: root.rows
          delegate: RowLayout {
            width: queueCol.width
            spacing: Style.space(14)

            Text {
              objectName: "title"
              Layout.preferredWidth: Style.space(80)
              text: modelData.waitsFor || ""
              color: root.waitColor
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }

            Text {
              objectName: "title"
              Layout.fillWidth: true
              text: root.esc(modelData.name || "") + " · " + root.esc(modelData.want || "")
              color: Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              wrapMode: Text.Wrap
            }
          }
        }
      }
    }

    Text {
      objectName: "empty"
      visible: root.rows.length === 0
      width: parent.width
      text: "no fronts waiting."
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.Wrap
    }
  }
}
