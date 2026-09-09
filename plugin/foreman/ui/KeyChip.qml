import QtQuick
import qs.Commons

// The mock's .key: a small accent-filled chip holding a letter. Static
// text in this task: the keys job wires behaviour later. Renders nothing
// when key is empty, so callers can bind model values unconditionally.
Rectangle {
  id: root

  property string key: ""

  visible: root.key !== ""
  implicitWidth: visible ? label.width + Style.space(10) : 0
  implicitHeight: visible ? label.height + Style.space(2) : 0
  width: implicitWidth
  height: implicitHeight

  color: Color.accent
  radius: 3

  Text {
    id: label
    anchors.centerIn: parent
    text: root.key
    color: Color.background
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
  }
}
