import QtQuick
import QtQuick.Layouts
import qs.Commons
import "." as Ui
import ".." as Foreman

// The mock's WORKING section: one card per front in Model.working.rows,
// in the model's order, then the merge desk card the mock shows at the
// end of the section.
//
// A front card shows the title row (front name, supervisor, progress
// line), the two-fill bar (built faint, landed solid), the task list (one
// row per task: state word, title, units, workers, elapsed/timeout or the
// dependency phrase), and the monitors line when the front has monitors.
// A front with nothing running shows a single muted sentence naming why
// instead of the task list. Card tone follows the front's condition:
// green while moving, red when stalled or halted, dim when idle or
// waiting on the owner.
//
// Where the mock shows a value the model does not carry, this renders
// what the model has: task counts, not the mock's summary numbers; the
// model's ledger rate, not the mock's; task titles in the dependency
// phrase, not the mock's wording. See the job summary for the gaps.
Item {
  id: root

  readonly property var rows: Foreman.Model.working.rows
  readonly property var desk: Foreman.Model.mergeQueue

  function esc(s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  }

  function numFmt(v) {
    if (v === undefined || v === null) return "?"
    if (Math.floor(v) === v) return String(Math.floor(v))
    return String(Math.round(v * 100) / 100)
  }

  function modelColor(name) {
    switch (String(name || "").toLowerCase()) {
      case "claude": return Ui.Palette.claude
      case "codex": return Ui.Palette.codex
      case "grok": return Ui.Palette.grok
      case "muse": return Ui.Palette.muse
      default: return Color.menu.text
    }
  }

  function runningJobs(task) {
    var out = []
    var jobs = task.jobs || []
    for (var i = 0; i < jobs.length; i++)
      if (jobs[i].state === "running") out.push(jobs[i])
    return out
  }

  function waitingJobs(task) {
    var out = []
    var jobs = task.jobs || []
    for (var i = 0; i < jobs.length; i++)
      if (jobs[i].state === "queued" || jobs[i].state === "planned") out.push(jobs[i])
    return out
  }

  function hasRunning(row) {
    var tasks = row.tasks || []
    for (var i = 0; i < tasks.length; i++)
      if (tasks[i].state === "active" || root.runningJobs(tasks[i]).length > 0) return true
    return false
  }

  function supervisorState(name) {
    var sid = Foreman.Model.supervisorFor(name)
    if (!sid) return ""
    var rec = Foreman.Model.roster[sid]
    return (rec && rec.state) || ""
  }

  // A human age as the status screen reads it: 14m, 2h. Never a timestamp.
  function ageWord(s) {
    s = Foreman.Model.liveAge(s)
    if (s === null || s === undefined) return "?"
    var total = Math.max(0, Math.floor(s))
    if (total < 60) return total + "s"
    if (total < 3600) return Math.floor(total / 60) + "m"
    if (total < 86400) return Math.floor(total / 3600) + "h"
    return Math.floor(total / 86400) + "d"
  }

  // The supervisor line: the name for a windowed session, and the same
  // three facts the status screen shows for a headless one — last wake
  // reason, turn count with last-turn age, and turn running while one is.
  function supervisorText(row) {
    var base = "· sup·" + row.name
    if (!row.isHeadless) return base
    var wake = row.wakeReason
      ? ("last wake '" + row.wakeReason + "' " + root.ageWord(row.wakeAgeS) + " ago")
      : "no wake yet"
    var turns = row.turnCount === 1 ? "1 turn"
      : root.numFmt(row.turnCount) + " turns"
    if (row.turnCount > 0)
      turns += " · last turn " + root.ageWord(row.lastTurnS) + " ago"
    var running = row.turnRunning ? " · turn running" : ""
    return base + " · " + wake + " · " + turns + running
  }

  // moving | stalled | halted | idle: the condition the tone, the
  // progress line and the empty-list sentence all read.
  function condition(row) {
    if (row.state === "halted") return "halted"
    if (root.supervisorState(row.name) === "stalled") return "stalled"
    if (root.hasRunning(row)) return "moving"
    return "idle"
  }

  function toneFor(row) {
    var c = root.condition(row)
    if (c === "halted" || c === "stalled") return "red"
    if (c === "moving") return "green"
    return "dim"
  }

  function progressColor(row) {
    var c = root.condition(row)
    if (c === "moving") return Ui.Palette.ok
    if (c === "halted" || c === "stalled") return Ui.Palette.bad
    return Color.menu.text
  }

  function pad2(n) {
    return (n < 10 ? "0" : "") + n
  }

  function hhmm(iso) {
    var d = new Date(iso)
    if (isNaN(d.getTime())) return ""
    return root.pad2(d.getUTCHours()) + ":" + root.pad2(d.getUTCMinutes())
  }

  function timeLeft(projectedFinish) {
    if (!projectedFinish) return ""
    var ms = Date.parse(projectedFinish) - Date.parse(Foreman.Model.now)
    if (isNaN(ms) || ms <= 0) return ""
    var mins = Math.round(ms / 60000)
    if (mins >= 60) return "~" + Math.round(mins / 60) + " h left"
    return "~" + mins + " min left"
  }

  function progressText(row) {
    var head = row.landed + " of " + row.total + " landed"
    if (root.condition(row) !== "moving") return head + " · " + root.condition(row)
    var tail = ""
    if (row.ratePerHour !== null && row.ratePerHour !== undefined)
      tail += " · " + Number(row.ratePerHour).toFixed(1) + " per hour"
    var left = root.timeLeft(row.projectedFinish)
    if (left !== "") tail += " · " + left
    return head + tail
  }

  function stateWord(state) {
    if (state === "landed" || state === "built") return "done"
    if (state === "active") return "running"
    if (state === "ready") return "queued"
    if (state === "waiting") return "waiting"
    return String(state || "")
  }

  function stateColor(word) {
    if (word === "done") return Ui.Palette.ok
    if (word === "running") return Color.accent
    if (word === "queued") return Ui.Palette.warn
    if (word === "waiting") return Color.muted
    return Color.menu.text
  }

  function taskTitle(row, id) {
    var tasks = row.tasks || []
    for (var i = 0; i < tasks.length; i++)
      if (tasks[i].id === id) return tasks[i].title || id
    return id
  }

  // One row per task: the state word is a separate Text; this is the
  // detail half as rich text so each worker's model name keeps its own
  // colour. Done tasks show the title alone: the model carries no
  // verification stamp or landing line for them.
  function taskHtml(row, task) {
    var word = root.stateWord(task.state)
    if (word === "done") return root.esc(task.title)
    var segs = []
    if (task.unitsTotal > 0 && task.unitsDone > 0)
      segs.push(root.esc(task.unitsDone + " of " + task.unitsTotal))
    var running = root.runningJobs(task)
    if (running.length > 0) {
      var seen = [], parts = []
      for (var i = 0; i < running.length; i++) {
        var m = running[i].model || "unknown"
        var known = false
        for (var k = 0; k < seen.length; k++)
          if (seen[k].model === m) { seen[k].n += 1; known = true }
        if (!known) seen.push({ model: m, n: 1 })
      }
      var total = 0
      for (var g = 0; g < seen.length; g++) {
        total += seen[g].n
        var chip = '<font color="' + root.modelColor(seen[g].model) + '">'
          + root.esc(seen[g].model) + "</font>"
        parts.push(seen[g].n > 1 ? seen[g].n + " " + chip : chip)
      }
      segs.push(parts.join(" + ") + (total > 1 ? " workers" : ""))
    }
    if (running.length === 1
        && running[0].elapsedS !== null && running[0].elapsedS !== undefined
        && running[0].timeoutS !== null && running[0].timeoutS !== undefined)
      segs.push(Math.floor(running[0].elapsedS / 60) + " of "
        + Math.floor(running[0].timeoutS / 60) + " min")
    var waiting = root.waitingJobs(task)
    if (running.length === 0 && waiting.length > 0) {
      var oldest = waiting[0]
      for (var w = 1; w < waiting.length; w++)
        if ((waiting[w].waitedS || 0) > (oldest.waitedS || 0)) oldest = waiting[w]
      var mins = Math.max(0, Math.round((Foreman.Model.liveAge(oldest.waitedS) || 0) / 60))
      var pool = Foreman.Model.poolFor(oldest.role || "")
      segs.push("waiting " + mins + " min for a " + root.esc(pool) + " slot")
    }
    if (word === "waiting" && task.after && task.after.length > 0) {
      var names = []
      for (var a = 0; a < task.after.length; a++)
        names.push(root.taskTitle(row, task.after[a]))
      segs.push("after " + root.esc(names.join(", ")))
    }
    return root.esc(task.title) + (segs.length > 0 ? " — " + segs.join(" · ") : "")
  }

  function monitorsText(row) {
    var parts = []
    var monitors = row.monitors || []
    for (var i = 0; i < monitors.length; i++) {
      var m = monitors[i]
      var piece = String(m.monitor) + " " + root.numFmt(m.value)
      if (m.of !== undefined && m.of !== null && m.of !== 0 && m.of !== 1)
        piece += "/" + root.numFmt(m.of)
      parts.push(piece)
    }
    return parts.length > 0 ? "metrics: " + parts.join(" · ") : ""
  }

  function problemKinds(name) {
    var out = []
    var rows = Foreman.Model.problems.rows || []
    for (var i = 0; i < rows.length; i++)
      if (rows[i].front === name && rows[i].kind) out.push(rows[i].kind)
    return out
  }

  function needsKinds(name) {
    var out = []
    var rows = Foreman.Model.needsYou.rows || []
    for (var i = 0; i < rows.length; i++) {
      var sender = Foreman.Model.roster[rows[i]["from"]] || ({})
      if (sender.front === name && rows[i].kind) out.push(rows[i].kind)
    }
    return out
  }

  function idleSentence(row) {
    var nk = root.needsKinds(row.name)
    if (nk.length > 0) return "waiting on your answer above (" + nk[0] + ")"
    var pk = root.problemKinds(row.name)
    if (pk.length > 0) return "nothing running · " + pk.join(", ") + " (see Problems)"
    return "nothing running."
  }

  function lastLanded() {
    var best = null
    var landed = (root.desk && root.desk.landed) || []
    for (var i = 0; i < landed.length; i++) {
      if (landed[i].landedS === null || landed[i].landedS === undefined) continue
      if (best === null || landed[i].landedS < best.landedS) best = landed[i]
    }
    return best
  }

  function deskStatus() {
    var waiting = ((root.desk && root.desk.waiting) || []).length
    var head = waiting === 1 ? "1 branch waiting" : waiting + " branches waiting"
    var last = root.lastLanded()
    if (last) {
      var when = root.hhmm(new Date(Date.parse(Foreman.Model.now) - last.landedS * 1000).toISOString())
      if (when !== "") return head + " · last landed " + when
    }
    return head + " · nothing landed yet"
  }

  function deskDetail() {
    var last = root.lastLanded()
    if (last) return last.branch + " → " + last.target
    var rows = ((root.desk && root.desk.rows) || [])
    if (rows.length > 0) return rows[0].branch + " → " + rows[0].target
    return "no branches yet"
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
      title: "Working"
      width: parent.width
    }

    Repeater {
      model: root.rows
      delegate: Card {
        property var front: modelData
        tone: root.toneFor(front)
        width: col.width

        RowLayout {
          width: parent.width
          spacing: Style.space(20)

          Text {
            objectName: "title"
            text: front.name
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.heading
          }

          Text {
            text: root.supervisorText(front)
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.title
          }

          Item { Layout.fillWidth: true }

          Text {
            text: root.progressText(front)
            color: root.progressColor(front)
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }
        }

        MeterBar {
          width: parent.width
          built: front.total > 0 ? (front.landed + front.built) / front.total : 0
          landed: front.total > 0 ? front.landed / front.total : 0
        }

        Column {
          visible: root.hasRunning(front)
          width: parent.width
          spacing: Style.space(2)

          Repeater {
            model: front.tasks
            delegate: Row {
              width: parent.width
              spacing: Style.space(14)

              Text {
                width: Style.space(80)
                text: root.stateWord(modelData.state)
                color: root.stateColor(root.stateWord(modelData.state))
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                objectName: "task"
                width: Math.max(0, parent.width - x)
                textFormat: Text.RichText
                text: root.taskHtml(front, modelData)
                color: Color.menu.text
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                wrapMode: Text.Wrap
              }
            }
          }
        }

        Text {
          visible: !root.hasRunning(front)
          width: parent.width
          text: root.idleSentence(front)
          color: Color.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          wrapMode: Text.Wrap
        }

        Text {
          objectName: "monitors"
          visible: root.monitorsText(front) !== ""
          width: parent.width
          text: root.monitorsText(front)
          color: Color.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }
      }
    }

    Card {
      tone: "plain"
      width: col.width

      RowLayout {
        width: parent.width
        spacing: Style.space(20)

        Text {
          objectName: "title"
          text: "merge desk"
          color: Color.menu.text
          font.family: Style.font.family
          font.pixelSize: Style.font.heading
        }

        Item { Layout.fillWidth: true }

        Text {
          text: root.deskStatus()
          color: Color.menu.text
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
      }

      Text {
        width: parent.width
        text: root.deskDetail()
        color: Color.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.Wrap
      }
    }

    Text {
      objectName: "empty"
      visible: root.rows.length === 0
      width: parent.width
      text: "nothing working."
      color: Color.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      wrapMode: Text.Wrap
    }
  }
}
