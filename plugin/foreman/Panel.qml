import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import qs.Commons
import "." as Foreman
import "ui" as Ui

// The Foreman panel: one full-screen layer-shell surface, summoned and
// dismissed by `omarchy-shell shell toggle foreman`. The mock's page
// (docs/mock/v0-tree.html): a centred column at the mock's max width and
// padding, the header row, one slot per section-13 block in the mock's
// order, and the footer row. Each slot is a Loader over its block file,
// guarded so a block that has not landed yet leaves an empty space rather
// than an error. Every block file is an Item that sizes itself to its
// content and reads Model directly. No colour is written here — every one
// comes from the shell's Color and Style singletons.
Item {
  id: root

  // Handed to a panel plugin by the shell's loader.
  property var shell: null
  property var manifest: null

  property bool opened: false

  readonly property string pluginId: (root.manifest && root.manifest.id) ? root.manifest.id : "foreman"

  // The slots in the supervisor's order. needsYou and problems land with
  // this job; working and the four queue/capacity blocks arrive from the
  // other layout jobs and drop in with no change here.
  readonly property var slots: [
    "ui/NeedsYouBlock.qml",
    "ui/ProblemsBlock.qml",
    "ui/WorkingBlock.qml",
    "ui/JobQueueBlock.qml",
    "ui/FrontQueueBlock.qml",
    "ui/MergeQueueBlock.qml",
    "ui/CapacityBlock.qml"
  ]

  function open(payloadJson) {
    root.opened = true
    Qt.callLater(function () { keys.forceActiveFocus() })
  }

  function close() {
    root.opened = false
  }

  function dismiss() {
    root.opened = false
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  PanelWindow {
    id: panel

    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore

    WlrLayershell.namespace: "foreman"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive

    // Shares the menu surface tokens, the way the emoji picker does: a theme
    // that restyles the menu restyles this panel with it.
    Rectangle {
      anchors.fill: parent
      color: Color.menu.background
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    Item {
      id: keys
      anchors.fill: parent
      focus: true
      // Every key runs through Keys: F freezes, ? opens the cheat
      // sheet, Esc backs out one level (cheat, pending letter, then
      // the panel), and two-letter hints fire their foreman verb.
      Keys.onPressed: (event) => {
        if (event.key === Qt.Key_Escape) {
          var back = Foreman.Keys.handleEscape()
          if (back === "close-panel") root.dismiss()
          event.accepted = true
        } else if (event.text === "?" || event.text === "f" || event.text === "F") {
          Foreman.Keys.handleText(event.text)
          event.accepted = true
        } else if (event.text.length === 1) {
          var outcome = Foreman.Keys.handleText(event.text)
          if (outcome !== "ignored") event.accepted = true
        }
      }
    }

    Flickable {
      anchors.fill: parent
      contentWidth: width
      contentHeight: page.height
      flickableDirection: Flickable.VerticalFlick
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: page
        width: Math.min(1100, parent.width - 64)
        anchors.horizontalCenter: parent.horizontalCenter
        topPadding: 36
        bottomPadding: 60
        spacing: Style.space(24)

        Ui.PanelHeader {
          width: parent.width
        }

        Repeater {
          model: root.slots
          delegate: Loader {
            width: page.width
            source: modelData
            active: true
            visible: status === Loader.Ready
            height: (status === Loader.Ready && item) ? item.height : 0
            onStatusChanged: if (status === Loader.Error) active = false
          }
        }

        RowLayout {
          width: parent.width
          spacing: Style.space(6)

          Ui.KeyChip { key: "F" }

          Text {
            text: "freeze everything"
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          Item { width: Style.space(12) }

          Ui.KeyChip { key: "?" }

          Text {
            text: "keys"
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          Item { width: Style.space(12) }

          Ui.KeyChip { key: "esc" }

          Text {
            text: "close"
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          Item { Layout.fillWidth: true }

          Text {
            text: "same text as foreman status"
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
        }
      }
    }

    // The ? cheat sheet over the page. Esc returns to the panel.
    Ui.CheatSheet {
      anchors.centerIn: parent
      width: Math.min(640, parent.width - 128)
      visible: Foreman.Keys.cheatOpen
    }
  }
}
