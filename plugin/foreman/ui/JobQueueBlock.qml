import QtQuick
import QtQuick.Layouts
import qs.Commons
import ".." as Foreman
import "." as Ui

// The mock's TASK QUEUE: one card listing the model queue oldest first. Per
// row the wait (yellow) or the muted word planned, then the job, its front,
// the pool it waits for, and the blocked phrase where one applies. Then the
// summary: waiting count, oldest wait, bottleneck pool over queued jobs.
// Both the row's left word and its body carry objectName "title" so
// panel --blocks reads rows as adjacent pairs, the summary last.
Item {
  id: root

  readonly property var rows: Foreman.Model.jobQueue.rows
  // The mock's yellow; binds accent until Palette carries status colours.
  readonly property color waitColor: Color.accent

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  }

  function ageText(s) {
    if (typeof s !== "number" || !(s >= 0)) return "?"
    var m = Math.floor(s / 60)
    if (m < 1) return Math.floor(s) + "s"
    if (m < 60) return m + " min"
    return Math.floor(m / 60) + "h " + (m % 60) + "m"
  }

  function colored(text, c) {
    return '<font color="' + String(c) + '">' + root.esc(text) + "</font>"
  }

  // Dot access: a dynamic Palette[pool] lookup does not resolve, so each
  // pool names its colour outright. opus renders in the claude colour, as
  // the mock paints it.
  function poolColor(pool) {
    if (pool === "opus") return Ui.Palette.claude
    if (pool === "codex") return Ui.Palette.codex
    if (pool === "grok") return Ui.Palette.grok
    if (pool === "muse") return Ui.Palette.muse
    return Color.menu.text
  }

  function capFor(pool) {
    var rows = Foreman.Model.capacity.rows
    for (var i = 0; i < rows.length; i++)
      if (rows[i].pool === pool) return rows[i]
    return null
  }

  function workingFor(front) {
    var rows = Foreman.Model.working.rows
    for (var i = 0; i < rows.length; i++)
      if (rows[i].name === front) return rows[i]
    return null
  }

  // The blocked phrase: planned rows carry the mock's spec wording (the
  // model does not project spec_path, so "spec written" is mock text);
  // queued rows carry the owner's open question and any anomaly on the
  // front. Rows with a blocked phrase omit the busy count, as the mock does.
  function extrasFor(row) {
    if (row.state === "planned") {
      var w = root.workingFor(row.front)
      if (w && w.state === "halted") return ["spec written, front halted"]
      return ["spec written, not yet queued"]
    }
    var out = []
    var front = root.workingFor(row.front)
    if (front && front.blockedOnOwner)
      for (var q = 0; q < front.blockedOnOwner.length; q++)
        out.push("blocked: " + front.blockedOnOwner[q])
    var problems = Foreman.Model.problems.rows
    for (var i = 0; i < problems.length; i++)
      if (problems[i].front === row.front || problems[i].subject === row.front)
        out.push("blocked: " + problems[i].kind)
    return out
  }

  function mainText(row) {
    var parts = [root.esc(row.title || row.task || ""), root.esc(row.front || "")]
    var extras = root.extrasFor(row)
    if (row.state === "queued") {
      var pool = Foreman.Model.poolFor(row.role || "")
      var need = "needs a " + root.colored(pool, root.poolColor(pool)) + " slot"
      if (extras.length === 0) {
        var cap = root.capFor(pool)
        if (cap) need += " (" + cap.held + (cap.total === null ? " busy" : " of " + cap.total + " busy") + ")"
      }
      parts.push(need)
    }
    for (var i = 0; i < extras.length; i++) parts.push(root.esc(extras[i]))
    return parts.join(" · ")
  }

  function summaryText() {
    var n = root.rows.length
    if (n === 0) return ""
    var oldest = 0
    var byPool = ({})
    for (var i = 0; i < n; i++) {
      if (root.rows[i].waitedS > oldest) oldest = root.rows[i].waitedS
      if (root.rows[i].state === "queued") {
        var p = Foreman.Model.poolFor(root.rows[i].role || "")
        byPool[p] = (byPool[p] || 0) + 1
      }
    }
    var out = n + " waiting · oldest " + root.ageText(oldest)
    var best = "", bestN = 0
    var pools = Object.keys(byPool).sort()
    for (var k = 0; k < pools.length; k++)
      if (byPool[pools[k]] > bestN) { bestN = byPool[pools[k]]; best = pools[k] }
    if (best !== "") out += " · " + best + " is the bottleneck (" + bestN + " waiting on it)"
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
      title: "Task queue"
      note: "planned jobs waiting to run, oldest first"
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
              text: modelData.state === "queued" ? root.ageText(modelData.waitedS) : "planned"
              color: modelData.state === "queued" ? root.waitColor : Color.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }

            Text {
              objectName: "title"
              Layout.fillWidth: true
              text: root.mainText(modelData)
              textFormat: Text.RichText
              color: Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              wrapMode: Text.Wrap
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
      text: "no jobs waiting."
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.Wrap
    }
  }
}
