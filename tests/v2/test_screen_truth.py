"""The screen and the install tell the truth.

Every test drives the real entry points against a fresh FOREMAN_STATE and
FOREMAN_CONFIG directory. Ledger lines written by hand below are
preconditions (a built task, a predecessor session), never the behavior
under test. The break each test catches is in its docstring.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from foreman import cli, fronts, paths, store
from foreman import collector as collector_module
from foreman import doctor as doctor_module
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.status import NOW_ENV
from foreman.verbs import checkpoint_main

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "packaging" / "foreman-post-merge"
PINNED_NOW = "2026-09-10T12:00:00+00:00"

#: Two chained tasks, so a close has something built and something left.
VALID_BRIEF = '''name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2

[[task]]
title = "first work"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check first"
size = 3
after = []

[[task]]
title = "second work"
scope = """
WHAT: do the second thing.
INPUTS: the first artifact.
OUTPUTS: the second artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check second"
size = 2
after = ["first work"]
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def write_brief(root: Path, name: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(VALID_BRIEF.format(name=name),
                                          encoding="utf-8")
    return brief_dir


def add_front(root: Path, name: str) -> None:
    assert cli.main(["front", "add", str(write_brief(root, name))]) == 0


def folded_tasks(name: str) -> dict[str, dict]:
    return {task["title"]: task for task in store.fold_by_id(
        store.read_ledger(paths.front_tasks_path(name)))}


def land_first_task(name: str, head: str) -> None:
    """Precondition: the front's first task landed at head, the way a
    landing always writes it."""
    tasks = store.read_ledger(paths.front_tasks_path(name))
    first = [task for task in tasks if task.get("title") == "first work"][-1]
    store.append_ledger(paths.front_tasks_path(name),
                        dict(first, state="landed", head=head,
                             landed_by="owner",
                             landed_at="2026-09-09T12:00:00+00:00"),
                        session_id="owner")


def build_first_task(name: str) -> None:
    """Precondition: the front's first task built, the way `task built`
    writes it."""
    tasks = store.read_ledger(paths.front_tasks_path(name))
    first = [task for task in tasks if task.get("title") == "first work"][-1]
    store.append_ledger(paths.front_tasks_path(name),
                        dict(first, state="built"),
                        session_id="owner")


def run_status(capsys) -> str:
    assert cli.main(["status"]) == 0
    return capsys.readouterr().out


def test_done_front_prints_once_in_done_and_nowhere_else(env, capsys):
    """A finished front reads once in Done and in no other block.

    Before the strip, a front at state done printed in Working with its
    tasks and monitors, and lent its ceiling to Capacity: three closed
    history records filled the screen a swarm is read from.
    """
    add_front(env, "alpha")
    add_front(env, "beta")
    capsys.readouterr()
    land_first_task("beta", "b" * 40)
    sha = folded_tasks("beta")["first work"]["head"]
    assert sha == "b" * 40
    assert cli.main(["front", "close", "beta"]) == 0
    capsys.readouterr()

    out = run_status(capsys)
    assert "Done:" in out
    assert out.count("Done:") == 1
    assert f"  beta \u2014 1/2 landed \u00b7 merge {sha} \u00b7 done " in out
    working = out.split("Working:\n", 1)[1].split("\nDone:\n", 1)[0]
    assert "beta" not in working
    assert "  alpha \u2014 " in working
    capacity = out.split("Capacity:\n", 1)[1].split("Overall:", 1)[0]
    assert "beta" not in capacity
    queue = out.split("Job queue:", 1)[1].split("Merge queue:", 1)[0]
    assert "beta" not in queue
    assert out.count("  beta \u2014 ") == 1
    assert "1 of 2 fronts moving (1 done)" in out
    for line in out.splitlines():
        assert len(line) <= 100


def test_front_close_merged_lands_built_tasks_with_the_sha(env, capsys):
    """`front close --merged` lands what the merge already landed.

    runtime-v2 read "7 built, 0 landed" after its PR merged: the work was
    on main and the ledger said it never landed. The sha travels onto
    every built task; tasks in any other state are left alone.
    """
    add_front(env, "gamma")
    capsys.readouterr()
    build_first_task("gamma")
    sha = "a" * 40
    assert cli.main(["front", "close", "gamma", "--merged", sha]) == 0
    out = capsys.readouterr().out
    assert "gamma closed" in out
    assert f"landed {sha}" in out

    tasks = folded_tasks("gamma")
    assert tasks["first work"]["state"] == "landed"
    assert tasks["first work"]["head"] == sha
    assert tasks["second work"]["state"] in ("ready", "waiting")
    assert "head" not in tasks["second work"]
    assert fronts.read_front_record("gamma")["state"] == "done"

    out = run_status(capsys)
    assert f"  gamma \u2014 1/2 landed \u00b7 merge {sha} \u00b7 done " in out


def test_front_close_merged_refuses_an_empty_sha(env, capsys):
    """`--merged` with no commit in it closes nothing: a landing with no
    head is exactly the lie this flag exists to end."""
    add_front(env, "gamma")
    capsys.readouterr()
    build_first_task("gamma")
    assert cli.main(["front", "close", "gamma", "--merged", ""]) == 1
    assert "--merged" in capsys.readouterr().err
    assert fronts.read_front_record("gamma")["state"] != "done"
    assert folded_tasks("gamma")["first work"]["state"] == "built"


def test_cap_change_pushes_a_collector_reload(env, capsys, monkeypatch):
    """`foreman cap` restarts the collector once its own write is durable,
    so the stale line clears with no hand restart."""
    calls = []
    monkeypatch.setattr(collector_module, "reload_after_config_change",
                        lambda: calls.append("reload"))
    assert cli.main(["cap", "muse", "3"]) == 0
    capsys.readouterr()
    assert calls == ["reload"]


def test_allocate_pushes_a_collector_reload(env, capsys, monkeypatch):
    """`front allocate` restarts the collector the way `cap` does: either
    ceiling moves the world the collector observes."""
    add_front(env, "alpha")
    capsys.readouterr()
    calls = []
    monkeypatch.setattr(collector_module, "reload_after_config_change",
                        lambda: calls.append("reload"))
    assert cli.main(["front", "allocate", "alpha", "muse", "5"]) == 0
    assert capsys.readouterr().out.strip() == "alpha: muse ceiling 5"
    assert calls == ["reload"]


def test_doctor_reports_a_cli_running_off_a_branch(env, capsys, monkeypatch):
    """An installed command on a branch is a divergence with its fix.

    The owner and every session call whatever this checkout has out, so a
    half-built branch is the runtime for the whole swarm until the install
    runs from the main-only checkout again.
    """
    monkeypatch.setattr(doctor_module, "_installed_cli_branch",
                        lambda: ("/repo/at/fault", "feature-x"))
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "doctor: foreman runs from branch 'feature-x'" in out
    assert "fix: reinstall foreman from the main-only checkout" in out
    assert "foreman collector restart" in out


def test_doctor_is_quiet_when_the_cli_runs_from_main(
        env, capsys, monkeypatch):
    """The same checkout on main is nothing to report: doctor stays clean
    on an empty state directory."""
    monkeypatch.setattr(doctor_module, "_installed_cli_branch",
                        lambda: ("/repo/at/main", "main"))
    assert cli.main(["doctor"]) == 0
    assert "doctor: clean" in capsys.readouterr().out


def test_doctor_is_quiet_when_no_installed_cli_answers(
        env, capsys, monkeypatch):
    """`python -m foreman` and in-process calls run their checkout by
    choice: with no installed command answering, there is no branch to be
    on the wrong one of."""
    monkeypatch.setattr(doctor_module, "_installed_cli_branch",
                        lambda: None)
    assert cli.main(["doctor"]) == 0
    assert "doctor: clean" in capsys.readouterr().out


def test_installed_cli_branch_reads_a_real_checkout(
        env, monkeypatch, tmp_path):
    """The branch comes from git, not from a guess: a real checkout on a
    real branch reports both, and anything but the installed command
    reports nothing."""
    repo = tmp_path / "pkg"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "screen-x", str(repo)],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    monkeypatch.setattr(doctor_module, "_package_checkout",
                        lambda: repo)
    monkeypatch.setattr("sys.argv", ["foreman", "doctor"])
    assert doctor_module._installed_cli_branch() == (str(repo), "screen-x")
    monkeypatch.setattr("sys.argv", ["pytest"])
    assert doctor_module._installed_cli_branch() is None


def _supervisor_on_roster(sid: str, front: str, state: str) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": {sid: {
        "id": sid, "role": "supervisor", "pool": "claude",
        "model": "claude-opus-5", "front": front, "job": None,
        "pid": None, "pgid": None, "worktree": "", "log": "",
        "timeout": "20m", "launched_by": "owner",
        "started_at": "2026-09-08T11:00:00+00:00",
        "last_declared_at": "2026-09-09T11:56:00+00:00",
        "cpu_s": 0.0, "state": state,
    }}})


def test_launch_supervisor_writes_the_supervisor_on_the_record(
        env, capsys, monkeypatch):
    """A summoned supervisor lands on the front record, not just the
    roster: the screen reads the record, and `front take` is the write
    being copied."""
    add_front(env, "alpha")
    capsys.readouterr()
    monkeypatch.chdir(env)
    monkeypatch.setattr(launch_module, "_start_supervisor",
                        lambda session_id, **kwargs: ([], "", 424242, None))
    monkeypatch.setattr(launch_module, "_confirm_started",
                        lambda pid: (777, None))
    assert cli.main(["launch", "supervisor", "alpha"]) == 0
    out = capsys.readouterr().out
    sid = [line for line in out.splitlines()
           if line.startswith("session: ")][0].split("session: ")[1].strip()
    roster = store.read_snapshot(paths.roster_path())["sessions"]
    assert roster[sid]["front"] == "alpha"
    assert roster[sid]["state"] == "running"
    assert fronts.read_front_record("alpha")["supervisor"] == sid


def test_live_successor_checkpoint_shows_on_the_screen(env, capsys,
                                                       monkeypatch):
    """The screen never reads "no checkpoint yet" for a live supervisor.

    A replaced predecessor still on the roster used to shadow the live
    successor: the roster scan returns the first entry, which checkpoints
    no more, while the session the front record names is checkpointing.
    """
    add_front(env, "alpha")
    capsys.readouterr()
    old, new = "ses-old0001", "ses-new0001"
    # The predecessor first: the roster scan returns it, and it
    # checkpoints no more, while the record names the live successor.
    _supervisor_on_roster(old, "alpha", "exited")
    first = store.read_snapshot(paths.roster_path())["sessions"][old]
    _supervisor_on_roster(new, "alpha", "starting")
    second = store.read_snapshot(paths.roster_path())["sessions"][new]
    store.write_snapshot(paths.roster_path(),
                         {"sessions": {old: first, new: second}})
    fronts.set_front_supervisor("alpha", new, by="owner")
    monkeypatch.setenv(SESSION_ENV, new)
    assert checkpoint_main("Polishing the lens", "Ship it", [], []) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)

    out = run_status(capsys)
    assert "doing now \u2014 Polishing the lens" in out
    assert "no checkpoint yet" not in out


def test_take_moves_the_supervisor_on_the_records(env, capsys, monkeypatch):
    """`front take` writes the record it takes and clears the one it
    leaves: one process, one identity, one front at a time, on the ledger
    as well as the roster."""
    add_front(env, "first")
    add_front(env, "second")
    capsys.readouterr()
    sid = "ses-move001"
    _supervisor_on_roster(sid, "first", "running")
    fronts.set_front_supervisor("first", sid, by="owner")
    assert cli.main(["front", "close", "first"]) == 0
    capsys.readouterr()
    monkeypatch.setenv(SESSION_ENV, sid)
    assert cli.main(["front", "take", "second"]) == 0
    capsys.readouterr()
    assert fronts.read_front_record("second")["supervisor"] == sid
    assert fronts.read_front_record("first").get("supervisor") is None


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=test@example.invalid",
         "-c", "user.name=foreman-test", *args],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    return proc.stdout.strip()


def _stub_bin(root: Path) -> tuple[Path, Path, Path]:
    """A bin dir with a recording `foreman` and a recording installer."""
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    calls = root / "foreman-calls"
    installed = root / "installed"
    (bindir / "foreman").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> {calls}\n',
        encoding="utf-8")
    (bindir / "fake-install").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> {installed}\n',
        encoding="utf-8")
    os.chmod(bindir / "foreman", 0o755)
    os.chmod(bindir / "fake-install", 0o755)
    return bindir, calls, installed


def _primary_with_main_checkout(root: Path) -> tuple[Path, Path]:
    """A primary checkout and a main-only checkout cloned from it."""
    primary = root / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    (primary / "src").mkdir()
    (primary / "src" / "app.py").write_text("V = 1\n", encoding="utf-8")
    _git(primary, "add", ".")
    _git(primary, "commit", "-qm", "seed")
    main = root / "main-only"
    subprocess.run(["git", "clone", "-q", str(primary), str(main)],
                   check=True)
    return primary, main


def _run_hook(primary: Path, main: Path, bindir: Path,
              extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, FOREMAN_MAIN_CHECKOUT=str(main),
               FOREMAN_INSTALL_CMD=str(bindir / "fake-install"),
               PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    env.update(extra or {})
    return subprocess.run(["sh", str(HOOK)], cwd=str(primary),
                          capture_output=True, text=True, env=env)


def test_post_merge_hook_fast_forwards_reinstalls_and_restarts(
        tmp_path):
    """A merge touching the sources moves the main checkout, refreshes the
    install, and restarts the collector: no hand restart anywhere."""
    primary, main = _primary_with_main_checkout(tmp_path)
    bindir, calls, installed = _stub_bin(tmp_path)
    old = _git(primary, "rev-parse", "HEAD")
    (primary / "src" / "app.py").write_text("V = 2\n", encoding="utf-8")
    _git(primary, "add", ".")
    _git(primary, "commit", "-qm", "two")
    _git(primary, "update-ref", "ORIG_HEAD", old)

    proc = _run_hook(primary, main, bindir)
    assert proc.returncode == 0
    assert _git(main, "rev-parse", "HEAD") == \
        _git(primary, "rev-parse", "HEAD")
    assert installed.read_text(encoding="utf-8").strip() == str(main)
    assert "collector\nrestart\n" in calls.read_text(encoding="utf-8")


def test_post_merge_hook_skips_the_reinstall_for_docs_only(tmp_path):
    """A merge outside the sources still moves the main checkout and
    restarts the collector, but reinstalls nothing."""
    primary, main = _primary_with_main_checkout(tmp_path)
    bindir, calls, installed = _stub_bin(tmp_path)
    old = _git(primary, "rev-parse", "HEAD")
    (primary / "notes.txt").write_text("words\n", encoding="utf-8")
    _git(primary, "add", ".")
    _git(primary, "commit", "-qm", "words")
    _git(primary, "update-ref", "ORIG_HEAD", old)

    proc = _run_hook(primary, main, bindir)
    assert proc.returncode == 0
    assert _git(main, "rev-parse", "HEAD") == \
        _git(primary, "rev-parse", "HEAD")
    assert not installed.exists()
    assert "collector\nrestart\n" in calls.read_text(encoding="utf-8")


def test_post_merge_hook_warns_and_restarts_without_a_main_checkout(
        tmp_path):
    """A hook with nowhere to fast-forward to says so and still restarts
    the collector: a hook never breaks the merge that fired it."""
    primary, main = _primary_with_main_checkout(tmp_path)
    bindir, calls, _installed = _stub_bin(tmp_path)
    proc = _run_hook(primary, main, bindir,
                     {"FOREMAN_MAIN_CHECKOUT": ""})
    assert proc.returncode == 0
    assert "FOREMAN_MAIN_CHECKOUT is not set" in proc.stderr
    assert "collector\nrestart\n" in calls.read_text(encoding="utf-8")
