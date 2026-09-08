import QtQuick
import QtQuick.Layouts
import qs.Commons
import ".." as Foreman

// The ? cheat sheet: every static key and what it does, then each
// two-letter hint with its verb. Esc closes it (Keys.handleEscape);
// no colour here is literal, every one binds a theme token.
Item {
  id: root

  implicitHeight: card.implicitHeight
  height: implicitHeight

  Card {
    id: card
    tone: "plain"
    width: parent.width

    Text {
      text: "Keys"
      color: Color.menu.text
      font.family: Style.font.family
      font.pixelSize: Style.font.heading
    }

    Column {
      width: parent.width
      spacing: Style.space(3)

      Row {
        width: parent.width
        spacing: Style.space(6)
        KeyChip { key: "F" }
        Text {
          text: "freeze everything"
          color: Color.menu.text
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(6)
        KeyChip { key: "?" }
        Text {
          text: "show this list"
          color: Color.menu.text
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(6)
        KeyChip { key: "esc" }
        Text {
          text: "back out one level, close at the top"
          color: Color.menu.text
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
      }

      Repeater {
        model: Foreman.Keys.cheatActions()
        delegate: Row {
          width: parent.width
          spacing: Style.space(6)
          KeyChip { key: modelData.hint }
          Text {
            width: Math.max(0, parent.width - x)
            text: modelData.label
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            wrapMode: Text.Wrap
          }
        }
      }
    }
  }
}
