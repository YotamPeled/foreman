import QtQuick
import qs.Commons
import "." as Ui

// The mock's .card: raised surface, 1px border, 6px radius, 14px/18px
// padding. tone draws the mock's 4px left bar for red/yellow/green and the
// dimmed look for dim; plain has no bar.
//
// Theme note: the shell's Color singleton carries no green and no yellow,
// so the mock's red, yellow and green bars bind the status colours on
// Palette, which holds the mock's values. Tone names are unchanged.
// Reached as Ui.Palette through a self-import: QtQuick exports its own
// Palette type, so the bare name does not reach our singleton.

//
// Contract for the other layout jobs and for panel --blocks: put the
// card's title in a Text with objectName "title". --blocks counts Cards
// via isForemanCard and reads titles off those Texts.
Rectangle {
  id: root

  property string tone: "plain" // plain | red | yellow | green | dim
  property bool isForemanCard: true

  default property alias content: body.data

  readonly property int padV: Style.space(14)
  readonly property int padH: Style.space(18)
  readonly property bool hasBar: root.tone === "red" || root.tone === "yellow" || root.tone === "green"

  color: Color.popups.background
  border.color: Color.menu.border
  border.width: 1
  radius: 6
  opacity: root.tone === "dim" ? 0.6 : 1.0

  implicitHeight: body.implicitHeight + root.padV * 2
  height: implicitHeight

  Rectangle {
    id: bar
    visible: root.hasBar
    width: Style.space(4)
    anchors {
      left: parent.left
      top: parent.top
      bottom: parent.bottom
    }
    radius: 2
    color: root.tone === "red" ? Ui.Palette.bad : root.tone === "yellow" ? Ui.Palette.warn : Ui.Palette.ok
  }

  Column {
    id: body
    anchors {
      left: parent.left
      right: parent.right
      top: parent.top
      leftMargin: root.padH + (root.hasBar ? bar.width : 0)
      rightMargin: root.padH
      topMargin: root.padV
    }
    spacing: Style.space(4)
  }
}
