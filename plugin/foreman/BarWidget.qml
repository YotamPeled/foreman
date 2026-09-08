import QtQuick
import qs.Commons
import "." as Foreman

// Placeholder bar item: the header numbers and nothing else. It must load
// without error and read the same model the panel does; the bar-widget job
// gives it its real shape.
Item {
  id: root

  property var shell: null
  property var manifest: null

  readonly property int registered: Foreman.Model.header.registered
  readonly property int observed: Foreman.Model.header.observed
  readonly property int needsYou: Foreman.Model.needsYou.count

  implicitWidth: label.implicitWidth
  implicitHeight: Style.bar.iconSlot

  Text {
    id: label
    anchors.centerIn: parent
    text: root.registered + "/" + root.observed
      + (root.needsYou > 0 ? " · " + root.needsYou : "")
    color: root.needsYou > 0 ? Color.bar.active : Color.bar.text
    font.family: Style.font.family
    font.pixelSize: Style.bar.iconFont
  }
}
