import Quickshell
import QtQuick
import "." as Foreman

// Main-thread stall probe. A 100 ms timer cannot be late unless something
// else held the thread; the lateness IS the block. Prints every gap over
// 50 ms and a summary, then exits.
//
// The block counts are printed at the start and at the end on purpose. A
// model that renders nothing never blocks, so speed alone proves nothing:
// the counts are what separates a fast panel from an empty one, and they
// must match the counts the same state folds to elsewhere.
//
// Run it through the harness rather than by hand:
//
//     bin/foreman-verify panel --stall [--state DIR]
//
// FOREMAN_PROBE_MS sets how long it watches (default 60000).
ShellRoot {
  id: probe
  property double last: 0
  property int over: 0
  property double worst: 0
  property double sum: 0
  property int ticks: 0
  property double began: 0
  readonly property int runMs: {
    var given = parseInt(Quickshell.env("FOREMAN_PROBE_MS"))
    return isNaN(given) || given <= 0 ? 60000 : given
  }

  function counts(tag) {
    var names = Foreman.Model.blockNames
    var out = []
    for (var i = 0; i < names.length; i++)
      out.push(names[i] + "=" + Foreman.Model.block(names[i]).count)
    console.log("PROBE counts " + tag + " " + out.join(" "))
  }

  Component.onCompleted: {
    probe.began = Date.now()
    probe.last = Date.now()
    console.log("PROBE start runMs=" + probe.runMs)
    // touch the model so it instantiates and starts polling
    probe.counts("at-start")
  }

  Timer {
    interval: 100; running: true; repeat: true
    onTriggered: {
      var now = Date.now()
      var gap = now - probe.last - 100
      probe.last = now
      probe.ticks++
      if (gap > 50) {
        probe.over++
        probe.sum += gap
        if (gap > probe.worst) probe.worst = gap
        console.log("STALL " + Math.round(now - probe.began) + "ms gap=" + Math.round(gap))
      }
      if (now - probe.began > probe.runMs) {
        probe.counts("at-end")
        console.log("PROBE done ticks=" + probe.ticks + " over50=" + probe.over
                    + " worst=" + Math.round(probe.worst)
                    + " blockedMs=" + Math.round(probe.sum))
        console.log("HARNESS-DONE")
        Qt.exit(0)
      }
    }
  }
}
