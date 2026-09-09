import QtQuick
import QtQuick.Layouts
import qs.Commons
import ".." as Foreman
import "." as Ui

// The mock's CAPACITY: a four-column grid of pool cards. Per pool the name
// in its model colour with the mock's filled square, busy-of-total slots,
// the meter line (or no-meter with the average), the average job time, and
// the yellow waiting count when any queued job waits on the pool. Waiting
// counts queued jobs only, the mock's own rule (its summary counts three
// on muse, not four). The name, busy line and third line carry
// objectName "title" so panel --blocks reads busy-of-total and the waiting
// count per pool.
Item {
  id: root

  // The mock's yellow; binds accent until Palette carries status colours.
  readonly property color waitColor: Color.accent

  // The mock's column order; pools the fixture never names append sorted.
  function orderedPools() {
    var want = ["opus", "codex", "grok", "muse"]
    var have = ({})
    var rows = Foreman.Model.capacity.rows
    for (var i = 0; i < rows.length; i++) have[rows[i].pool] = rows[i]
    var out = []
    for (var w = 0; w < want.length; w++)
      if (have[want[w]]) { out.push(have[want[w]]); delete have[want[w]] }
    var rest = Object.keys(have).sort()
    for (var r = 0; r < rest.length; r++) out.push(have[rest[r]])
    return out
  }

  readonly property var pools: root.orderedPools()

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

  function busyText(row) {
    if (row.total === null || row.total === undefined) return row.held + " slots busy"
    return row.held + " of " + row.total + " slots busy"
  }

  function mins(s) {
    return Math.round(s / 60)
  }

  // The mock words the codex average as reviews, every other pool as jobs.
  // typeof guard: a null average must not coerce through >= 0.
  function avgText(row) {
    if (typeof row.avgS !== "number" || !(row.avgS >= 0)) return ""
    return "avg " + (row.pool === "codex" ? "review" : "job") + " " + root.mins(row.avgS) + " min"
  }

  function allSupervisors(pool) {
    var found = false, ok = true
    var lines = Foreman.Model.slots
    for (var i = 0; i < lines.length; i++) {
      if (!lines[i] || lines[i].pool !== pool) continue
      found = true
      if (lines[i].role !== "supervisor") ok = false
    }
    return found && ok
  }

  function avgOrSupervisors(row) {
    var avg = root.avgText(row)
    if (avg !== "") return avg
    if (root.allSupervisors(row.pool)) return "supervisors only"
    return "no average yet"
  }

  function clockText(iso) {
    var t = new Date(Date.parse(iso || ""))
    if (isNaN(t.getTime())) return "?"
    function pad(n) { return (n < 10 ? "0" : "") + n }
    return pad(t.getUTCHours()) + ":" + pad(t.getUTCMinutes())
  }

  function meterText(row) {
    if (row.meter === null || row.meter === undefined) {
      var avg = root.avgText(row)
      return "no meter" + (avg !== "" ? " · " + avg : "")
    }
    return Math.round(row.meter * 100) + "% of 5-hour window · resets " + root.clockText(row.resetsAt)
  }

  function waitingFor(pool) {
    var n = 0
    var rows = Foreman.Model.jobQueue.rows
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].state !== "queued") continue
      if (Foreman.Model.poolFor(rows[i].role || "") === pool) n += 1
    }
    return n
  }

  function thirdVisible(row) {
    return root.waitingFor(row.pool) > 0 || (row.meter !== null && row.meter !== undefined)
  }

  function thirdText(row) {
    var w = root.waitingFor(row.pool)
    if (w > 0) return w === 1 ? "1 job waiting for a slot" : w + " jobs waiting for a slot"
    return root.avgOrSupervisors(row)
  }

  function thirdColor(row) {
    return root.waitingFor(row.pool) > 0 ? root.waitColor : Color.muted
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
      title: "Capacity"
      width: parent.width
    }

    Grid {
      id: grid
      width: parent.width
      columns: 4
      columnSpacing: Style.space(10)
      rowSpacing: Style.space(10)
      visible: root.pools.length > 0

      Repeater {
        model: root.pools
        delegate: Card {
          tone: "plain"
          width: (grid.width - 3 * Style.space(10)) / 4

          Text {
            objectName: "title"
            width: parent.width
            text: "■ " + (modelData.pool || "")
            color: root.poolColor(modelData.pool || "")
            font.family: Style.font.family
            font.pixelSize: Style.font.heading
            elide: Text.ElideRight
          }

          Text {
            objectName: "title"
            width: parent.width
            text: root.busyText(modelData)
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            wrapMode: Text.Wrap
          }

          Text {
            width: parent.width
            text: root.meterText(modelData)
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }

          Text {
            objectName: "title"
            width: parent.width
            visible: root.thirdVisible(modelData)
            text: root.thirdText(modelData)
            color: root.thirdColor(modelData)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }
        }
      }
    }

    Text {
      objectName: "empty"
      visible: root.pools.length === 0
      width: parent.width
      text: "no pools."
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.Wrap
    }
  }
}
