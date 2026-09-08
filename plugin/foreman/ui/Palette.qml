pragma Singleton
import QtQuick

// The four model colours the mock names. Vendor brand colours, not theme
// colours: this file is the one place a literal colour may appear in the
// plugin. Everything else binds Color.* and Style.*.
QtObject {
  id: root

  readonly property color claude: "#d97757"
  readonly property color codex: "#10a37f"
  readonly property color grok: "#e8e8e8"
  readonly property color muse: "#6b9fe8"
}
