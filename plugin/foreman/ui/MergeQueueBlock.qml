import QtQuick
import QtQuick.Layouts
import qs.Commons
import ".." as Foreman

// The mock's MERGE QUEUE: one card, merging rows first, then waiting, then
// landed (the model's own three group arrays, in that order). The left
// column carries the state word in the mock's colours. The body is a flow
// of three fragments so the target keeps the mock's bold through
// font.bold: RichText <b> crashes the offscreen renderer, while the
// NeedsYou block's bold Text renders fine. Unlanded rows read branch,
// from, bold target, review and age; landed rows read branch, from,
// target, landing clock and review, muted. Then the summary: merging and
// waiting counts with the average handoff-to-landed time. The mock's clean
// landing ratio is not in the model, so it is omitted. The left word and
// each body fragment carry objectName "title": panel --blocks joins a
// row's fragments back into its sentence, the summary last.
Item {
  id: root

  readonly property var merging: Foreman.Model.mergeQueue.merging
  readonly property var waiting: Foreman.Model.mergeQueue.waiting
  readonly property var landed: Foreman.Model.mergeQueue.landed
  readonly property var rows: root.merging.concat(root.waiting, root.landed)
  // The mock's yellow; binds accent until Palette carries status colours.
  readonly property color waitColor: Color.accent

  function ageText(s) {
    if (typeof s !== "number" || !(s >= 0)) return "?"
    var m = Math.floor(s / 60)
    if (m < 1) return Math.floor(s) + "s"
    if (m < 60) return m + " min"
    return Math.floor(m / 60) + "h " + (m % 60) + "m"
  }

  function stateColor(state) {
    if (state === "merging") return Color.accent
    if (state === "waiting") return root.waitColor
    return Color.muted
  }

  function clockText(landedS) {
    if (typeof landedS !== "number" || !(landedS >= 0)) return "?"
    var t = new Date(Date.parse(Foreman.Model.now) - landedS * 1000)
    if (isNaN(t.getTime())) return "?"
    function pad(n) { return (n < 10 ? "0" : "") + n }
    return pad(t.getUTCHours()) + ":" + pad(t.getUTCMinutes())
  }

  function reviewText(row) {
    var reviews = row.reviews || []
    if (reviews.length > 0) {
      var names = []
      for (var i = 0; i < reviews.length; i++) names.push(reviews[i])
      return "reviewed by " + names.join(", ")
    }
    return row.state === "landed" ? "no review" : "no review requested"
  }

  function preText(row) {
    var head = String(row.branch || "")
    if (row.state === "landed") return head + " · " + String(row.from || "") + " → "
    return head + " · from " + String(row.from || "") + " → "
  }

  function postText(row) {
    if (row.state === "landed")
      return " · " + root.clockText(row.landedS) + " · " + root.reviewText(row)
    var age = row.state === "merging"
      ? "started " + root.ageText(row.requestedS) + " ago"
      : "waited " + root.ageText(row.requestedS)
    return " · " + root.reviewText(row) + " · " + age
  }

  function rowBold(row) {
    return row.state !== "landed"
  }

  function rowColor(row) {
    return row.state === "landed" ? Color.muted : Color.menu.text
  }

  function summaryText() {
    if (root.rows.length === 0) return ""
    var out = root.merging.length + " merging · " + root.waiting.length + " waiting"
    var total = 0, n = 0
    for (var i = 0; i < root.landed.length; i++) {
      var row = root.landed[i]
      if (typeof row.requestedS === "number" && typeof row.landedS === "number"
          && row.requestedS >= row.landedS) {
        total += row.requestedS - row.landedS
        n += 1
      }
    }
    if (n > 0) out += " · avg " + root.ageText(total / n) + " from handoff to landed"
    return out
  }

  implicitHeight: col.implicitHeight
  height: implicitHeight

  Column {
    id: col
    anchors {
      left: parent.left
      right: parent.right
      top: parent.top
    }
    spacing: Style.space(10)

    SectionHeading {
      title: "Merge queue"
      note: "branches handed to the merge desk"
      width: parent.width
    }

    Card {
      tone: "plain"
      width: col.width
      visible: root.rows.length > 0

      Column {
        id: queueCol
        width: parent.width
        spacing: Style.space(3)

        Repeater {
          model: root.rows
          delegate: RowLayout {
            width: queueCol.width
            spacing: Style.space(14)

            Text {
              objectName: "title"
              Layout.preferredWidth: Style.space(80)
              text: modelData.state || ""
              color: root.stateColor(modelData.state)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }

            Flow {
              Layout.fillWidth: true
              spacing: 0

              Text {
                objectName: "title"
                text: root.preText(modelData)
                color: root.rowColor(modelData)
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }

              Text {
                objectName: "title"
                text: modelData.target || ""
                color: root.rowColor(modelData)
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                font.bold: root.rowBold(modelData)
              }

              Text {
                objectName: "title"
                text: root.postText(modelData)
                color: root.rowColor(modelData)
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }
            }
          }
        }

        Text {
          objectName: "title"
          width: parent.width
          text: root.summaryText()
          color: Color.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }
      }
    }

    Text {
      objectName: "empty"
      visible: root.rows.length === 0
      width: parent.width
      text: "nothing at the merge desk."
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.Wrap
    }
  }
}
