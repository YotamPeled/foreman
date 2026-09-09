import Quickshell
import QtQuick
import "." as Foreman

// The model with no window on it: `bin/foreman-verify panel --loads` runs
// this under QT_QPA_PLATFORM=offscreen with FOREMAN_STATE pointing at a
// fixture, and reads the block counts off stdout. Nothing here draws, so a
// failure is a failure of the data layer and of nothing else.
//
// The model reads one summary file the runtime wrote, on a watch with a
// 2 second poll under it, so the first load can land a beat after startup.
// Rather than sleep a fixed time and hope, this settles: it prints once the
// eight counts and the row facts have stopped moving, and gives up printing
// whatever it has at the deadline.
ShellRoot {
  id: harness

  property string signature: ""
  property int stable: 0
  property int ticks: 0

  readonly property int settleTicks: 4     // 4 x 250ms unchanged
  readonly property int deadlineTicks: 40  // 10s, then print regardless

  function counts() {
    var names = Foreman.Model.blockNames
    var out = []
    for (var i = 0; i < names.length; i++)
      out.push(names[i] + " " + Foreman.Model.block(names[i]).count)
    return out
  }

  // Row-level facts the live-shape assertions read. One per line,
  // `DETAIL <key> <value>` with the value running to end of line; every
  // value here is a single line in the fixture. Counts prove the fold;
  // these prove the seven live-shape fixes (checkpoint doing-now, title
  // task refs, built-in-remaining, role-to-pool waiting, job models,
  // front-joined questions, ledger rate with its window).
  function details() {
    var out = []
    var fronts = Foreman.Model.fronts
    for (var f = 0; f < fronts.length; f++) {
      var row = fronts[f]
      out.push("front." + row.name + ".doingNow " + row.doingNow)
      out.push("front." + row.name + ".headless " + row.isHeadless)
      out.push("front." + row.name + ".wakeReason " + row.wakeReason)
      out.push("front." + row.name + ".wakeAgeS " + Foreman.Model.liveAge(row.wakeAgeS))
      out.push("front." + row.name + ".turnCount " + row.turnCount)
      out.push("front." + row.name + ".lastTurnS " + Foreman.Model.liveAge(row.lastTurnS))
      out.push("front." + row.name + ".doingAgeS " + Foreman.Model.liveAge(row.doingAgeS))
      out.push("front." + row.name + ".turnRunning " + row.turnRunning)
      out.push("front." + row.name + ".remaining " + row.remaining.join("|"))
      out.push("front." + row.name + ".blockedOnOwner " + row.blockedOnOwner.join("|"))
      out.push("front." + row.name + ".ratePerHour " + row.ratePerHour)
      out.push("front." + row.name + ".rateWindowH " + row.rateWindowH)
      out.push("front." + row.name + ".projectedFinish " + row.projectedFinish)
      for (var t = 0; t < row.tasks.length; t++) {
        var jobs = []
        for (var j = 0; j < row.tasks[t].jobs.length; j++)
          jobs.push(row.tasks[t].jobs[j].id)
        out.push("task." + row.name + "." + row.tasks[t].id + ".jobs " + jobs.join("|"))
      }
    }
    var head = Foreman.Model.header
    out.push("header.collectorAt " + head.collectorAt)
    out.push("header.collectorAgeS " + head.collectorAgeS)
    var need = Foreman.Model.needsYou
    for (var n = 0; n < need.rows.length; n++)
      out.push("needsYou." + need.rows[n].id + ".waitedS "
               + Foreman.Model.liveAge(need.rows[n].waitedS))
    var probs = Foreman.Model.problems
    for (var p = 0; p < probs.rows.length; p++)
      out.push("problems." + (probs.rows[p].subject || p) + ".sinceS "
               + Foreman.Model.liveAge(probs.rows[p].sinceS))
    var cap = Foreman.Model.capacity
    for (var c = 0; c < cap.rows.length; c++)
      out.push("capacity." + cap.rows[c].pool + ".waiting " + cap.rows[c].waiting)
    var queue = Foreman.Model.jobQueue
    for (var q = 0; q < queue.rows.length; q++) {
      out.push("job." + queue.rows[q].id + ".model " + queue.rows[q].model)
      out.push("job." + queue.rows[q].id + ".waitedS "
               + Foreman.Model.liveAge(queue.rows[q].waitedS))
    }
    var merges = Foreman.Model.mergeQueue
    for (var m = 0; m < merges.rows.length; m++) {
      out.push("merge." + merges.rows[m].id + ".requestedS "
               + Foreman.Model.liveAge(merges.rows[m].requestedS))
      out.push("merge." + merges.rows[m].id + ".landedS "
               + Foreman.Model.liveAge(merges.rows[m].landedS))
    }
    // The queue holds only planned and queued jobs, so the models of
    // running and finished jobs are reported off their task rows instead.
    for (var a = 0; a < fronts.length; a++)
      for (var b = 0; b < fronts[a].tasks.length; b++)
        for (var c2 = 0; c2 < fronts[a].tasks[b].jobs.length; c2++)
          out.push("job." + fronts[a].tasks[b].jobs[c2].id + ".model "
                   + fronts[a].tasks[b].jobs[c2].model)
    return out
  }

  function report(settled) {
    var lines = harness.counts()
    for (var i = 0; i < lines.length; i++) console.log("BLOCK " + lines[i])
    console.log("NONEMPTY " + Foreman.Model.nonEmptyBlocks())
    var facts = harness.details()
    for (var d = 0; d < facts.length; d++) console.log("DETAIL " + facts[d])
    console.log("STATEDIR " + Foreman.Model.stateDir)
    console.log(settled ? "HARNESS-DONE settled" : "HARNESS-DONE deadline")
    Qt.exit(0)
  }

  Timer {
    interval: 250
    running: true
    repeat: true
    onTriggered: {
      harness.ticks += 1
      // Settle on the counts AND the row facts: the summary is replaced
      // atomically while this runs, and reporting on settled counts alone
      // could print a doing-now from a file that has since been rewritten.
      var current = harness.counts().join(",") + "\n" + harness.details().join("\n")
      if (current === harness.signature) harness.stable += 1
      else { harness.signature = current; harness.stable = 0 }
      if (harness.ticks >= harness.settleTicks && harness.stable >= harness.settleTicks) {
        running = false
        harness.report(true)
      } else if (harness.ticks >= harness.deadlineTicks) {
        running = false
        harness.report(false)
      }
    }
  }
}
