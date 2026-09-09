import QtQuick
import qs.Commons

// The mock's .btn: a filled label. tone is plain for neutral actions
// (decline, relaunch), green for approve, red for destructive actions
// (kill). The mock's green fill binds the theme highlight; there is no
// green token in Color, and a literal would break themes.
Rectangle {
  id: root

  property string label: ""
  property string tone: "plain" // plain | green | red

  implicitWidth: pillLabel.width + Style.space(20)
  implicitHeight: pillLabel.height + Style.space(4)
  width: implicitWidth
  height: implicitHeight

  radius: 4
  color: root.tone === "green" ? Color.accent : root.tone === "red" ? Color.urgent : Color.muted

  Text {
    id: pillLabel
    anchors.centerIn: parent
    text: root.label
    color: Color.background
    font.family: Style.font.family
    font.pixelSize: Style.font.title
  }
}
