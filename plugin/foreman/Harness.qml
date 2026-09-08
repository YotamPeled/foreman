import Quickshell
import QtQuick
import "." as Foreman

// The model with no window on it: `bin/foreman-verify panel --loads` runs
// this under QT_QPA_PLATFORM=offscreen with FOREMAN_STATE pointing at a
// fixture, and reads the block counts off stdout. Nothing here draws, so a
// failure is a failure of the data layer and of nothing else.
//
// Front discovery is a directory scan and the file reads are watched, so the
// counts arrive over the first few hundred milliseconds. Rather than sleep a
// fixed time and hope, this settles: it prints once the eight counts have
// stopped moving, and gives up printing whatever it has at the deadline.
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

  function report(settled) {
    var lines = harness.counts()
    for (var i = 0; i < lines.length; i++) console.log("BLOCK " + lines[i])
    console.log("NONEMPTY " + Foreman.Model.nonEmptyBlocks())
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
      var current = harness.counts().join(",")
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
