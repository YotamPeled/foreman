.pragma library

// Every read of a Foreman state file goes through here. Two shapes: a JSON
// snapshot (roster.json, observed.json, collector.json) and an append-only
// JSONL ledger where a later line supersedes an earlier one with the same id.

function snapshot(text, fallback) {
  if (typeof text !== "string" || text.length === 0) return fallback
  try {
    var value = JSON.parse(text)
    return (value === null || value === undefined) ? fallback : value
  } catch (e) {
    return fallback
  }
}

// A ledger is written a line at a time and read while it is being written, so
// the last line can be half written. Drop what will not parse rather than
// losing every line before it.
function records(text) {
  var out = []
  if (typeof text !== "string" || text.length === 0) return out
  var lines = text.split("\n")
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (line.length === 0) continue
    try {
      var record = JSON.parse(line)
      if (record && typeof record === "object") out.push(record)
    } catch (e) {
      // A truncated tail line, or a line from a future writer.
    }
  }
  return out
}

// Last line wins, first appearance keeps its place: the order a supervisor
// wrote its queue in is the order the panel shows it in.
function foldBy(list, keyOf) {
  var order = []
  var byKey = ({})
  for (var i = 0; i < list.length; i++) {
    var key = keyOf(list[i])
    if (key === null || key === undefined || key === "") continue
    if (!(key in byKey)) order.push(key)
    byKey[key] = list[i]
  }
  var out = []
  for (var j = 0; j < order.length; j++) out.push(byKey[order[j]])
  return out
}

function foldById(list) {
  return foldBy(list, function (record) {
    return typeof record.id === "string" ? record.id : ""
  })
}

// Anomalies carry no id: a resolution appends a revised copy of the same
// (kind, subject) pair, so that pair is the identity.
function foldAnomalies(list) {
  return foldBy(list, function (record) {
    if (typeof record.kind !== "string" || typeof record.subject !== "string")
      return ""
    return record.kind + " " + record.subject
  })
}

// Rows whose field is still unset: an inbox item nobody answered, an anomaly
// nobody resolved, a merge that has not landed.
function unset(list, field) {
  var out = []
  for (var i = 0; i < list.length; i++) {
    var value = list[i][field]
    if (value === null || value === undefined || value === "") out.push(list[i])
  }
  return out
}

function ledger(text) { return foldById(records(text)) }

function seconds(fromIso, nowIso) {
  var from = Date.parse(fromIso)
  var now = nowIso ? Date.parse(nowIso) : Date.now()
  if (isNaN(from) || isNaN(now)) return null
  return (now - from) / 1000
}

function sortByKey(list, field) {
  var copy = list.slice()
  copy.sort(function (a, b) {
    var x = a[field] || ""
    var y = b[field] || ""
    return x < y ? -1 : (x > y ? 1 : 0)
  })
  return copy
}
