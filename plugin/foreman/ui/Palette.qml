pragma Singleton
import QtQuick

// The four model colours the mock names. Vendor brand colours, not theme
// colours: this file is the one place a literal colour may appear in the
// plugin. Everything else binds Color.*, Style.* or Palette.*.
//
// The three status colours the mock names, with the mock's values. The
// shell's Color singleton carries no green and no yellow, and these carry
// meaning the theme has no token for (done against running against
// queued, a pool with waits against one without), so they earn their
// place the same way the model colours do. Named for what they mean,
// not for what they look like.
QtObject {
  id: root

  readonly property color claude: "#d97757"
  readonly property color codex: "#10a37f"
  readonly property color grok: "#e8e8e8"
  readonly property color muse: "#6b9fe8"

  readonly property color ok: "#a7c080"
  readonly property color warn: "#dbbc7f"
  readonly property color bad: "#e67e80"
}
