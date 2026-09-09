import QtQuick
import qs.Commons

// The mock's h2: small caps, wide letter spacing, muted, with an optional
// trailing note in the smaller muted style the mock uses on TASK QUEUE and
// MERGE QUEUE. title is uppercased here so callers pass plain words.
Item {
  id: root

  property string title: ""
  property string note: ""

  implicitHeight: row.implicitHeight
  height: implicitHeight

  Row {
    id: row
    anchors {
      left: parent.left
      right: parent.right
    }
    height: implicitHeight
    spacing: Style.space(8)

    Text {
      text: root.title.toUpperCase()
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.subtitle
      font.letterSpacing: 2
    }

    Text {
      visible: root.note !== ""
      text: root.note !== "" ? "— " + root.note : ""
      color: Color.muted
      opacity: 0.7
      font.family: Style.font.family
      font.pixelSize: Style.font.title
      width: Math.max(0, parent.width - x)
      elide: Text.ElideRight
    }
  }
}
