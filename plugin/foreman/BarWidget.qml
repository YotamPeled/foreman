import QtQuick
import qs.Commons
import "." as Foreman

// The header numbers on the bar: sessions registered/observed, whether the
// swarm is frozen, how many things need the owner, how many anomalies are
// open. It reads the same Model singleton the panel reads; there is no
// second reader of the state directory.
//
// Bar idiom, not panel idiom: numbers, not sentences (the sentences live in
// the hover tooltip). Urgent while anything needs the owner or an anomaly
// is open, dimmed when the swarm is quiet. Frozen is a mark, never a
// count, so it reads without counting. Left click summons the panel.
//
// The root declares the three properties the bar host injects by name
// (bar, moduleName, settings) and nothing else, so this loads both in the
// bar and headlessly. No colour is written here: every one comes from the
// shell's Color tokens, the way WidgetButton resolves them.
Item {
  id: root

  property var bar: null
  property string moduleName: "foreman"
  property var settings: ({})

  readonly property int registered: Foreman.Model.header.registered
  readonly property int observed: Foreman.Model.header.observed
  readonly property bool frozen: Foreman.Model.header.frozen
  readonly property int needsYou: Foreman.Model.needsYou.count
  readonly property int anomalies: Foreman.Model.problems.count

  readonly property bool urgent: root.needsYou > 0 || root.anomalies > 0
  readonly property bool quiet: !root.urgent && !root.frozen

  // WidgetButton's own resolution: the bar's tokens when hosted, the
  // theme tokens when not.
  readonly property color foreground: root.bar ? root.bar.barForeground : Color.foreground
  readonly property color urgentColor: root.bar ? root.bar.urgent : Color.urgent

  readonly property string shortText: {
    var s = root.registered + "/" + root.observed
    if (root.frozen) s += " ❄"
    if (root.needsYou > 0) s += " !" + root.needsYou
    if (root.anomalies > 0) s += " ⚠" + root.anomalies
    return s
  }

  readonly property string tipText: {
    var s = root.registered + " registered, " + root.observed + " observed"
    s += root.frozen ? " · frozen" : " · not frozen"
    s += root.needsYou === 1 ? " · 1 needs you" : " · " + root.needsYou + " need you"
    s += root.anomalies === 1 ? " · 1 open anomaly" : " · " + root.anomalies + " open anomalies"
    return s
  }

  function hideTip() {
    if (root.bar && typeof root.bar.hideTooltip === "function") root.bar.hideTooltip(root)
  }

  implicitWidth: label.implicitWidth + Style.space(16)
  implicitHeight: Style.bar.sizeHorizontal
  opacity: root.quiet ? 0.45 : 1

  Behavior on opacity {
    NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
  }

  Text {
    id: label
    anchors.centerIn: parent
    text: root.shortText
    textFormat: Text.PlainText
    color: root.urgent ? root.urgentColor : root.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    renderType: Text.NativeRendering
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
  }

  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onEntered: {
      if (root.bar && typeof root.bar.showTooltip === "function")
        root.bar.showTooltip(root, root.tipText)
    }
    onExited: root.hideTip()
    onClicked: function (mouse) {
      root.hideTip()
      if (mouse.button !== Qt.LeftButton) return
      if (!root.bar || typeof root.bar.run !== "function") return
      root.bar.run("omarchy-shell shell toggle foreman")
    }
  }
}
