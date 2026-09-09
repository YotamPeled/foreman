import QtQuick
import qs.Commons

// The mock's .bar: a track with two overlaid fills. built is the faint
// accent fill, landed the solid one; each is a fraction from 0 to 1 and is
// clamped here so model values cannot overflow the track.
Item {
  id: root

  property real built: 0
  property real landed: 0

  function fraction(v) {
    if (!(v > 0)) return 0
    if (!(v < 1)) return 1
    return v
  }

  implicitHeight: Style.space(8)
  height: implicitHeight

  Rectangle {
    anchors.fill: parent
    radius: 4
    color: Color.background
  }

  Rectangle {
    width: parent.width * root.fraction(root.built)
    anchors {
      left: parent.left
      top: parent.top
      bottom: parent.bottom
    }
    radius: 4
    color: Color.accent
    opacity: 0.5
  }

  Rectangle {
    width: parent.width * root.fraction(root.landed)
    anchors {
      left: parent.left
      top: parent.top
      bottom: parent.bottom
    }
    radius: 4
    color: Color.accent
  }
}
