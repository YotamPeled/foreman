"""A queued node's page is rendered from the node, the map and the sheet.

Every test drives the real CLI (or ``launch.role_sheet`` /
``launch.render_node_page``) against a fresh FOREMAN_STATE with a
v5-shape front whose repository is named ``foreman``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, launch, paths, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.launch import start_queued
from foreman.pools import _common as pool_common

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5"
target = "main"
check = "python -m pytest tests -q"
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def iso(moment: datetime) -> str:
    return moment.isoformat()


def make_bare(root: Path) -> tuple[Path, Path, str]:
    src = root / "src-repo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    bare = root / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(bare)],
                   check=True)
    return src, bare, sha


def write_v5(root: Path, name: str, url: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url), encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape") -> str:
    from foreman import fronts

    monkeypatch.delenv(SESSION_ENV, raising=False)
    src, bare, sha = make_bare(env)
    assert cli.main(["front", "add", str(write_v5(env, name, str(bare)))]) == 0
    capsys.readouterr()
    record = fronts.read_front_record(name)
    work = (record or {}).get("repositories", [{}])[0].get("work") or "v5"
    subprocess.run(
        ["git", "-C", str(src), "branch", work, "main"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/remotes/origin/{work}"],
        check=True)
    return sha


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def session_entry(sid, role, front):
    return entities.Session.from_dict({
        "id": sid, "role": role, "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }).to_dict()


def seed_roster(*entries):
    store.write_snapshot(
        paths.roster_path(),
        {"sessions": {entry["id"]: entry for entry in entries}})


def seed_supervisor(front, sid=SUP):
    seed_roster(session_entry(sid, "supervisor", front))


def add_argv(front="v5shape", parent="v5shape", kind="milestone",
             title="Front inputs", **flags):
    argv = ["node", "add", front, "--parent", parent, "--kind", kind,
            "--title", title,
            "--verify", flags.get("verify", "python -m pytest tests -q"),
            "--must-not-touch", flags.get("must_not_touch",
                                          "the live state directory"),
            "--reason", flags.get("reason", "the tree needs this node"),
            "--break", flags.get("break_", "admit a job as a parent"),
            "--repo", flags.get("repo", "foreman")]
    if flags.get("role") is not None:
        argv.extend(["--role", flags["role"]])
    if flags.get("what") is not None:
        argv.extend(["--what", flags["what"]])
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def add_chain(monkeypatch, capsys, job_id="job-a", role="builder",
              what="implement the door", reason=None,
              must_not_touch=None, verify=None):
    mil = add_ok(monkeypatch, capsys, title="milestone one",
                 node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    flags = dict(parent=tsk, kind="job", title="implement the door",
                 role=role, node_id=job_id, what=what)
    if reason is not None:
        flags["reason"] = reason
    if must_not_touch is not None:
        flags["must_not_touch"] = must_not_touch
    if verify is not None:
        flags["verify"] = verify
    job = add_ok(monkeypatch, capsys, **flags)
    return mil, tsk, job


def seed_fact(front, fact_id, text, basis="seen", refs=None):
    store.append_ledger(
        paths.front_map_path(front),
        entities.MapFact(
            id=fact_id, front=front, repo="foreman",
            section="Launch", text=text, basis=basis,
            refs=list(refs or []),
            seen_at="2026-09-10T00:00:00+00:00",
            seen_where="src/foreman/launch.py",
            commit="abc1234",
            by=SUP,
        ).to_dict(),
        session_id=SUP,
    )


HAND_SPEC = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
    "Do not touch src/foreman/store.py.\n"
)


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "seed"], check=True)
    return path


@pytest.fixture()
def launch_spawn(env, monkeypatch):
    """Capture ``cmd_launch`` and refuse a real systemd unit."""
    calls: list = []

    def fake_spawn(argv, *, pid_path, session_id, popen):
        pid = 2_000_000 + len(calls)
        Path(pid_path).write_text(f"{pid}\n", encoding="utf-8")
        calls.append({"kind": "spawn", "argv": list(argv),
                      "session": session_id})
        return pid

    real = launch_module.cmd_launch

    def wrapped(args):
        calls.append({"kind": "launch", "args": args})
        return real(args)

    monkeypatch.setattr(pool_common, "spawn_and_wait", fake_spawn)
    monkeypatch.setattr(launch_module, "cmd_launch", wrapped)
    return calls


def folded_tree(front="v5shape"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def test_builder_sheet_is_second_person_and_names_the_rules():
    """The runtime's builder sheet says how the role works."""
    text = launch.role_sheet("builder", {})
    assert text.startswith("You are the builder.")
    assert "Commit as you go" in text
    assert "would go red without the work" in text
    assert "No path under any user's home directory" in text
    assert "exit" in text.lower()


def test_sheet_add_appears_under_the_supervisor_heading():
    """Whoever queues may add to the default; the add sits under its heading."""
    text = launch.role_sheet("builder", {"sheet_add": "read the map first"})
    assert "You are the builder." in text
    assert "## Added by the supervisor" in text
    heading_at = text.index("## Added by the supervisor")
    assert "read the map first" in text[heading_at:]
    assert text.index("You are the builder.") < heading_at


def test_sheet_replace_without_a_reason_is_refused():
    """A full replacement with no recorded reason names the field."""
    with pytest.raises(launch.Refused) as caught:
        launch.role_sheet("builder", {"sheet_replace": "do something else"})
    assert "sheet_reason" in str(caught.value)


def test_sheet_replace_with_a_reason_replaces_the_default():
    """With a reason, the replacement is the whole sheet; the default is gone."""
    text = launch.role_sheet("builder", {
        "sheet_replace": "You are a specialist. Exit when done.",
        "sheet_reason": "this node is not a default builder job",
        "sheet_add": "this add must not appear",
    })
    assert text.strip() == "You are a specialist. Exit when done."
    assert "You are the builder." not in text
    assert "Added by the supervisor" not in text
    assert "this add must not appear" not in text


def test_node_revise_sheet_replace_without_reason_is_refused(
        env, monkeypatch, capsys):
    """--sheet-replace without --sheet-reason names the flag and writes nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, job = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, [
        "node", "revise", "v5shape", job,
        "--sheet-replace", "You are a specialist.",
        "--reason", "try a replacement",
    ], SUP) == 1
    _, err = capsys.readouterr()
    assert "sheet-reason" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before


def test_node_revise_sheet_replace_with_reason_writes_both(
        env, monkeypatch, capsys):
    """--sheet-replace TEXT --sheet-reason TEXT stores both on the fold."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, job = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    assert run(monkeypatch, [
        "node", "revise", "v5shape", job,
        "--sheet-replace", "You are a specialist. Exit when done.",
        "--sheet-reason", "this node is not a default builder job",
        "--reason", "replace the sheet",
    ], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == job
    node = folded_tree()[job]
    assert node["sheet_replace"] == "You are a specialist. Exit when done."
    assert node["sheet_reason"] == "this node is not a default builder job"
    sheet = launch.role_sheet(node["role"], node)
    assert "You are the builder." not in sheet
    assert "You are a specialist." in sheet


def test_rendered_page_contains_what_must_not_touch_verify_branch_and_facts(
        env, monkeypatch, capsys):
    """The page for a queued builder node is the node, the branch and the facts it names."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    seed_fact("v5shape", "fct-alpha01",
              "the worker page is rendered from the node",
              refs=[{"ref": "origin/v5", "sha": "deadbeefcafebabe"}])
    seed_fact("v5shape", "fct-beta002",
              "a job with no sheet is refused", basis="assumed")
    _mil, _tsk, job = add_chain(
        monkeypatch, capsys, job_id="job-a",
        what="Build the page. Names fct-alpha01.",
        reason="Decision 7 cites fct-beta002.",
        must_not_touch="src/foreman/store.py",
        verify="python -m pytest tests -q")
    node = folded_tree()[job]
    branch = "job/v5shape-job-a"
    page = launch.render_node_page(
        "v5shape", node, repo="foreman", branch=branch,
        session_id="ses-page001", worktree="/wt", target="v5",
        scratch="/scratch", log="/log", verdict="/verdict",
        timeout="20m")
    assert "Build the page. Names fct-alpha01." in page
    assert "Must not touch: src/foreman/store.py" in page
    assert "python -m pytest tests -q" in page
    assert f"- branch: {branch}" in page
    assert "- repository: foreman" in page
    assert "fct-alpha01" in page
    assert "the worker page is rendered from the node" in page
    assert "[seen]" in page
    assert "origin/v5 = deadbeefcafebabe" in page
    assert "fct-beta002" in page
    assert "a job with no sheet is refused" in page
    assert "[assumed]" in page
    assert "You are the builder." in page
    assert "## This job (written by the supervisor)" in page
    assert "## Map facts" in page


def test_node_naming_no_fact_prints_the_no_facts_line(
        env, monkeypatch, capsys):
    """A node that names no fct- id prints the no-facts line, not silence."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, job = add_chain(
        monkeypatch, capsys, job_id="job-a",
        what="Build the page with no fact ids.")
    node = folded_tree()[job]
    page = launch.render_node_page(
        "v5shape", node, repo="foreman", branch="job/v5shape-job-a",
        session_id="ses-page002")
    assert "no map facts named" in page
    assert "## Map facts" in page


def test_start_queued_writes_the_node_page_as_the_spec(
        env, monkeypatch, capsys, launch_spawn):
    """start_queued calls render_node_page instead of a three-field brief."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    seed_fact("v5shape", "fct-alpha01", "rendered from the node")
    _mil, _tsk, node_id = add_chain(
        monkeypatch, capsys, job_id="job-a",
        what="Cut the oldest ready job from the queue. See fct-alpha01.")
    assert run(monkeypatch, ["job", "queue", "v5shape", node_id], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    job_id, reason = start_queued("v5shape", node_id, by="collector")
    assert reason == "", reason
    launched = [item["args"] for item in launch_spawn
                if item.get("kind") == "launch"]
    assert len(launched) == 1
    spec = Path(launched[0].spec).read_text(encoding="utf-8")
    node = folded_tree()[node_id]
    expected = launch.render_node_page(
        "v5shape", node, repo=str(node.get("repo") or "foreman"),
        branch=launched[0].branch, session_id="queued",
        job=job_id, timeout=launched[0].timeout or "",
        target=launched[0].base or "", kind="implement")
    assert spec == expected
    assert "Cut the oldest ready job from the queue." in spec
    assert "fct-alpha01" in spec
    assert "rendered from the node" in spec
    assert f"- branch: {launched[0].branch}" in spec
    assert "You are the builder." in spec


def test_hand_launch_page_is_byte_identical_for_the_same_spec(
        env, monkeypatch, capsys):
    """The hand launch still wraps the spec through the pool role template."""
    repo = make_repo(env / "repo-hand")
    spec_path = env / "hand-spec.md"
    spec_path.write_text(HAND_SPEC, encoding="utf-8")
    written: dict[str, str] = {}
    real = launch_module.write_no_symlink

    def capture(path, text):
        written[Path(path).name] = text
        return real(path, text)

    monkeypatch.setattr(launch_module, "write_no_symlink", capture)
    wt = env / "wt-hand"
    rc = run(monkeypatch, [
        "launch", "grok", "grok", str(spec_path),
        "--repo", str(repo), "--worktree", str(wt), "--dry-run",
        "--front", "demo",
    ])
    out, err = capsys.readouterr()
    assert rc == 0, err
    page = written["FOREMAN-JOB.md"]
    session_id = None
    for line in out.splitlines():
        if line.startswith("session: "):
            session_id = line.split("session: ", 1)[1].strip()
    assert session_id
    branch = f"foreman/{session_id}"
    target = "main"
    scratch = str(paths.session_scratch_dir(session_id))
    log_path = str(paths.session_log_path(session_id))
    verdict = str(paths.session_verdict_path(session_id))
    timeout = "20m"
    env_block = launch.environment_block(
        str(wt), branch, target, scratch, log_path, verdict, timeout)
    rulings = launch.format_rulings(launch.read_rulings("demo"))
    role_text = launch.render_worker_prompt(
        role="grok", kind="implement", front="demo", supervisor="demo",
        session_id=session_id, verdict_path=verdict,
        rulings=rulings, environment=env_block)
    expected = launch.build_job_file(
        "(none)", "implement", "(none)", "demo",
        env_block, rulings, "(no task scope recorded)",
        HAND_SPEC, "0", role_text=role_text)
    assert page == expected
    assert HAND_SPEC in page
    assert "## Map facts" not in page
    assert "Added by the supervisor" not in page
    assert "- repository:" not in page
