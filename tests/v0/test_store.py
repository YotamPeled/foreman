"""Storage layer: concurrency, atomicity, truncation tolerance."""

import json
import multiprocessing as mp

from foreman import paths, store


def _append_many(ledger: str, session_id: str, count: int, tag: str) -> None:
    for i in range(count):
        store.append_ledger(ledger, {"tag": tag, "i": i}, session_id=session_id)


def test_concurrent_appends_lose_no_line(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    ledger = paths.rulings_path()
    ctx = mp.get_context("fork")
    workers = [
        ctx.Process(target=_append_many, args=(str(ledger), f"ses-{k}", 100, f"p{k}"))
        for k in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
    assert all(worker.exitcode == 0 for worker in workers)

    raw = ledger.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 200
    rows = [json.loads(line) for line in raw]
    assert {(row["tag"], row["i"]) for row in rows} == {
        (f"p{k}", i) for k in range(2) for i in range(100)
    }
    assert all("at" in row and "by" in row for row in rows)


def _rewrite_snapshot(path: str, count: int) -> None:
    for i in range(count):
        store.write_snapshot(path, {"n": i, "payload": "x" * 1000})


def test_snapshot_readers_see_complete_values(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    snapshot = paths.observed_path()
    ctx = mp.get_context("fork")
    writer = ctx.Process(target=_rewrite_snapshot, args=(str(snapshot), 200))
    writer.start()
    try:
        seen = set()
        for _ in range(5000):
            value = store.read_snapshot(snapshot, default=None)
            if value is None:
                continue
            assert set(value) == {"n", "payload"}
            assert value["payload"] == "x" * 1000
            assert isinstance(value["n"], int) and 0 <= value["n"] < 200
            seen.add(value["n"])
            if len(seen) > 1:
                break
        assert seen
    finally:
        writer.join(60)
    assert writer.exitcode == 0
    assert store.read_snapshot(snapshot) == {"n": 199, "payload": "x" * 1000}


def test_truncated_final_line_reads_complete_lines(tmp_path):
    ledger = tmp_path / "events.jsonl"
    complete = [{"n": 1}, {"n": 2, "items": [1, 2, 3]}, {"n": 3, "s": "x"}]
    ledger.write_text(
        "".join(json.dumps(row) + "\n" for row in complete) + '{"n": 4, "half',
        encoding="utf-8",
    )
    assert store.read_ledger(ledger) == complete


def test_missing_files_return_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    assert store.read_ledger(paths.merges_path()) == []
    assert store.read_snapshot(paths.roster_path(), default={"a": 1}) == {"a": 1}
    assert store.read_snapshot(paths.roster_path()) is None


def test_append_stamps_only_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    ledger = paths.inbox_path()
    entry = store.append_ledger(
        ledger, {"at": "2001-01-01T00:00:00+00:00", "by": "ses-x", "q": 1}
    )
    assert entry == {"at": "2001-01-01T00:00:00+00:00", "by": "ses-x", "q": 1}
    assert store.read_ledger(ledger) == [entry]


def _stamp_many(path: str, key: str, count: int) -> None:
    def change(roster):
        roster = roster or {"sessions": {}}
        entry = roster["sessions"].setdefault(key, {})
        entry["n"] = entry.get("n", 0) + 1
        return roster

    for _ in range(count):
        store.update_snapshot(path, change, default={"sessions": {}})


def test_concurrent_snapshot_edits_lose_no_update(tmp_path, monkeypatch):
    """Two writers editing one snapshot must not overwrite each other.

    write_snapshot alone cannot do this: it locks only the rename, so both
    writers read the same old value and the second discards the first.
    """
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    roster = paths.roster_path()
    store.write_snapshot(roster, {"sessions": {}})
    ctx = mp.get_context("fork")
    writers = [
        ctx.Process(target=_stamp_many, args=(str(roster), f"ses-{k}", 60))
        for k in range(3)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(60)
    assert all(writer.exitcode == 0 for writer in writers)
    sessions = store.read_snapshot(roster)["sessions"]
    assert {key: entry["n"] for key, entry in sessions.items()} == {
        f"ses-{k}": 60 for k in range(3)
    }
