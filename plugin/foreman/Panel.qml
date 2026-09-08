import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import "." as Foreman

// The Foreman panel: one full-screen layer-shell surface, summoned and
// dismissed by `omarchy-shell shell toggle foreman`. This job builds the
// surface and proves the feed reaches it; the layout job replaces the rows
// below with the real cards. No colour is written here — every one comes
// from the shell's Color and Style singletons, so a theme change repaints
// the panel with no work on our side.
Item {
  id: root

  // Handed to a panel plugin by the shell's loader.
  property var shell: null
  property var manifest: null

  property bool opened: false

  readonly property string pluginId: (root.manifest && root.manifest.id) ? root.manifest.id : "foreman"

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

  // One row per section 13 block: the name, and how many rows came back.
  readonly property var blocks: [
    { name: "header", label: "header", count: Foreman.Model.header.count },
    { name: "needsYou", label: "needs you", count: Foreman.Model.needsYou.count },
    { name: "problems", label: "problems", count: Foreman.Model.problems.count },
    { name: "working", label: "working", count: Foreman.Model.working.count },
    { name: "jobQueue", label: "job queue", count: Foreman.Model.jobQueue.count },
    { name: "frontQueue", label: "front queue", count: Foreman.Model.frontQueue.count },
    { name: "mergeQueue", label: "merge queue", count: Foreman.Model.mergeQueue.count },
    { name: "capacity", label: "capacity", count: Foreman.Model.capacity.count }
  ]

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
      Keys.onEscapePressed: root.dismiss()
    }

    Column {
      anchors.centerIn: parent
      spacing: Style.spacing.sm

      Text {
        text: "foreman"
        color: Color.accent
        font.family: Style.font.family
        font.pixelSize: Style.font.title
      }

      Text {
        text: Foreman.Model.stateDir
        color: Color.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        bottomPadding: Style.spacing.sm
      }

      Repeater {
        model: root.blocks

        Row {
          spacing: Style.spacing.md

          Text {
            text: modelData.label
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            width: Style.space(160)
          }

          Text {
            text: String(modelData.count)
            color: modelData.count > 0 ? Color.menu.text : Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }
        }
      }
    }
  }
}
