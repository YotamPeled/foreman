import QtQuick
import qs.Commons
import qs.Ui
import "." as Foreman

// The header numbers on the bar: sessions registered/observed, whether the
// swarm is frozen, how many things need the owner, how many anomalies are
// open. It reads the same Model singleton the panel reads; there is no
// second reader of the state directory.
//
// Bar idiom, not panel idiom: numbers, not sentences (the sentences live in
// the tooltip). Urgent while anything needs the owner or an anomaly is open,
// dimmed when the swarm is quiet. Frozen is a mark, never a count, so it
// reads without counting. Left click summons the panel, the way the menu
// widget summons the menu. No colour is written here: every one comes from
// the shell's Color through WidgetButton.
BarWidget {
  id: root
  moduleName: "foreman"

  readonly property int registered: Foreman.Model.header.registered
  readonly property int observed: Foreman.Model.header.observed
  readonly property bool frozen: Foreman.Model.header.frozen
  readonly property int needsYou: Foreman.Model.needsYou.count
  readonly property int anomalies: Foreman.Model.problems.count

  readonly property bool urgent: root.needsYou > 0 || root.anomalies > 0
  readonly property bool quiet: !root.urgent && !root.frozen

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

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.shortText
    active: root.urgent
    dimmed: root.quiet
    tooltipText: root.tipText
    onPressed: function (which) {
      if (which !== Qt.LeftButton) return
      if (!root.bar || typeof root.bar.run !== "function") return
      root.bar.run("omarchy-shell shell toggle foreman")
    }
  }
}
