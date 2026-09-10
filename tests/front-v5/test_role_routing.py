"""Decision 38: a queued job names a role, never a pool; the runtime picks
the pool from the front's team and reroutes when that pool is out.

Under the owner's pool-outage roster Muse builds in Grok's place,
behaviour nodes included, so a rerouted builder node is not refused for
being non-mechanical. Nothing here launches a worker.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from foreman import cli, fronts, launch, paths, progress, store
from foreman.caller import SESSION_ENV

V5_BRIEF = '''\
name = "routes"
goal = "A front whose builders come from two pools."
finish-line = "A queued builder starts on whichever builder pool is in."
decisions = ["Everything is a CLI verb."]
supervisor = "opus-5:high"
team = [
  "opus-5:high:1:supervisor",
  "grok-4.6:high:2:builder",
  "muse-spark:xhigh:3:builder",
  "opus-5:high:1:backup-builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "wroutes"
target = "main"
check = "true"
land = "push"
'''

NODE_ARGS = ["--kind", "job", "--title", "a leaf", "--verify", "true",
             "--must-not-touch", "x", "--reason", "y", "--break", "z",
             "--repo", "foreman"]


def git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.email=test@example.invalid",
                    "-c", "user.name=foreman-test", *args],
                   check=True, capture_output=True)


@pytest.fixture()
def front(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    git("init", "-b", "main", "-q", str(tmp_path))
    git("-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init")
    src = tmp_path / "src-repo"
    src.mkdir()
    git("init", "-b", "main", "-q", str(src))
    git("-C", str(src), "commit", "-q", "--allow-empty", "-m", "init")
    bare = tmp_path / "remote.git"
    git("clone", "--bare", "-q", str(src), str(bare))
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "brief.toml").write_text(V5_BRIEF.format(url=bare),
                                      encoding="utf-8")
    assert fronts.front_add_main(str(brief)) == 0
    tree = paths.front_tree_path("routes")
    tree.parent.mkdir(parents=True, exist_ok=True)
    rows = ({"id": "mil-1", "front": "routes", "parent": "routes",
             "kind": "milestone", "title": "one", "state": ""},
            {"id": "job-1", "front": "routes", "parent": "mil-1",
             "kind": "job", "title": "a behaviour leaf", "role": "builder",
             "repo": "foreman", "verify": "true", "must_not_touch": "x",
             "reason": "y", "break": "z", "mechanical": False,
             "state": "queued"})
    with tree.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return tmp_path


def put_out(pool: str) -> None:
    until = datetime.now(timezone.utc) + timedelta(days=30)
    store.append_ledger(paths.pools_path(), {
        "id": pool, "pool": pool, "out_until": until.isoformat(),
        "because": "billing"})


def job_1() -> dict:
    return next(n for n in store.fold_by_id(store.read_ledger(
        paths.front_tree_path("routes"))) if n.get("id") == "job-1")


def run(argv: list[str]) -> int:
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


BUILT_BY_8_1A = pytest.mark.xfail(
    strict=True, reason="job-8.1a builds this; its worker removes this marker")


def test_builder_takes_the_first_builder_pool_when_it_is_in(front):
    record = fronts.read_front_record("routes")
    assert launch._team_pool(record, job_1()) == "grok"


@BUILT_BY_8_1A
def test_builder_reroutes_to_the_next_builder_pool_when_the_first_is_out(front):
    put_out("grok")
    record = fronts.read_front_record("routes")
    assert launch._team_pool(record, job_1()) == "muse"


def test_a_rerouted_behaviour_node_is_not_refused_as_not_mechanical(front):
    put_out("grok")
    record = fronts.read_front_record("routes")
    assert progress.muse_mechanical_refusal(record, job_1()) is None


@BUILT_BY_8_1A
def test_no_pool_for_the_role_leaves_the_job_without_a_pool(front):
    put_out("grok")
    put_out("muse")
    record = fronts.read_front_record("routes")
    assert launch._team_pool(record, job_1()) is None


@BUILT_BY_8_1A
def test_a_v5_node_naming_a_pool_role_is_refused(front, capsys):
    assert run(["node", "add", "routes", "--parent", "mil-1", *NODE_ARGS,
                "--role", "grok"]) != 0
    err = capsys.readouterr().err
    assert "refused" in err and "grok" in err


def test_a_v5_node_naming_a_team_role_is_admitted(front):
    assert run(["node", "add", "routes", "--parent", "mil-1", *NODE_ARGS,
                "--role", "builder"]) == 0


MUSE_ONLY_BRIEF = V5_BRIEF.replace('  "grok-4.6:high:2:builder",\n', '').replace(
    'name = "routes"', 'name = "museonly"').replace('wroutes', 'wmuseonly')


@BUILT_BY_8_1A
def test_a_muse_builder_takes_behaviour_nodes_where_muse_replaces_grok(front):
    # Owner ruling rul-j734hyt (2026-09-10): Grok is out for good and Muse
    # replaces it everywhere, so a team whose only builder is Muse builds
    # behaviour nodes too.
    bare = front / "remote.git"
    brief = front / "brief-museonly"
    brief.mkdir()
    (brief / "brief.toml").write_text(MUSE_ONLY_BRIEF.format(url=bare),
                                      encoding="utf-8")
    assert fronts.front_add_main(str(brief)) == 0
    record = fronts.read_front_record("museonly")
    node = dict(job_1(), front="museonly")
    assert launch._team_pool(record, node) == "muse"
    assert progress.muse_mechanical_refusal(record, node) is None


@BUILT_BY_8_1A
def test_the_working_team_gives_a_muse_only_builder_slots_for_behaviour_nodes(front):
    # rul-j734hyt: with Muse the only builder, decision 31's mechanical-share
    # threshold may not leave the front with no builder at all.
    from foreman import team as team_mod
    bare = front / "remote.git"
    brief = front / "brief-museteam"
    brief.mkdir()
    (brief / "brief.toml").write_text(
        MUSE_ONLY_BRIEF.replace("museonly", "museteam").format(url=bare),
        encoding="utf-8")
    assert fronts.front_add_main(str(brief)) == 0
    record = fronts.read_front_record("museteam")
    tree = [dict(row, front="museteam") for row in store.fold_by_id(
        store.read_ledger(paths.front_tree_path("routes")))]
    derived = team_mod.derive_team(record, tree, [])
    builders = [e for e in derived
                if (e.get("role") if isinstance(e, dict) else e.role) == "builder"]
    counts = [(e.get("count") if isinstance(e, dict) else e.count) for e in builders]
    assert builders and max(counts) >= 1, derived
