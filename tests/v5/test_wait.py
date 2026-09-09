"""`foreman wait` reads the job ledger, never the log.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``, inheriting no ``FOREMAN_*``
variable. A fake collector is a thread appending a terminal line through
``store.append_ledger``. No test opens a window or a systemd unit.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from foreman import cli, paths, store
from foreman import launch as launch_module
from foreman import wait as wait_module
from foreman.caller import SESSION_ENV
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter

FRONT = "alpha"
SID = "ses-wait0001"
JOB = "job-wait001"
JOB_A = "job-wait-a"
JOB_B = "job-wait-b"
WORKER = "ses-wrk0001"
LOG_MARKER = "### finished rc=0\n"

SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
)


class FakeAdapter(PoolAdapter):
    """Fake pool: writes the pid file and returns, spawning nothing real."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    def launch(self, ctx: LaunchContext) -> int:
        pid = os.getpid()
        ctx.pid_path.write_text(f"{pid}\n", encoding="utf-8")
        return pid

    def observe(self, session: Session) -> dict:
        return {"transcript_mtime": None, "cpu_s": 0.0,
                "finish_present": False, "finish_rc": None}

    def command_str(self, ctx: LaunchContext) -> str:
        return f"fake-exec --prompt-file {ctx.job_path}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.setattr(wait_module, "POLL_SECONDS", 0.05)
    return tmp_path


@pytest.fixture()
def fake_pool():
    from foreman import pools

    pools.register("fake", FakeAdapter())
    try:
        yield FakeAdapter()
    finally:
        pools.unregister("fake")


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", "seed.txt")
    run("commit", "-qm", "seed")
    return path


def seat(entries: dict[str, dict]) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": entries})


def worker_session(sid: str, **fields) -> dict:
    base = {
        "id": sid, "role": "muse", "pool": "fake",
        "model": "fake-test-model", "front": FRONT,
        "job": JOB, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": None, "started_at": None,
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return Session.from_dict(base).to_dict()


def seed_job(job_id: str, **fields) -> dict:
    record = {
        "id": job_id, "task": "tas-wait01", "kind": "implement",
        "role": "muse", "state": "running", "branch": "job/wait",
        "log": "", "session": SID, "front": FRONT,
    }
    record.update(fields)
    store.append_ledger(paths.front_jobs_path(FRONT), record)
    return record


def complete(job_id: str, **changes) -> None:
    """Append a revised job line the way the collector does."""
    records = [line for line in store.read_ledger(paths.front_jobs_path(FRONT))
               if line.get("id") == job_id]
    latest = records[-1]
    store.append_ledger(paths.front_jobs_path(FRONT), dict(latest, **changes))


def expected_line(job_id: str, outcome: str, exit_s: str, log: str) -> str:
    return (f"{job_id} {outcome} exit={exit_s} "
            f"branch=job/wait head=? log={log}")


def test_wait_on_a_session_returns_the_collectors_line(
        env, monkeypatch, capsys):
    """A session id blocks until a terminal job line lands, then prints
    that line and exits with the worker's code."""
    log = str(env / "worker.log")
    Path(log).write_text("still running\n", encoding="utf-8")
    seat({SID: worker_session(SID, log=log)})
    seed_job(JOB, log=log)
    threading.Thread(
        target=lambda: (time.sleep(0.2),
                        complete(JOB, state="returned", exit_code=0)),
        daemon=True,
    ).start()

    assert cli.main(["wait", SID]) == 0
    assert capsys.readouterr().out.strip() == expected_line(
        JOB, "returned", "0", log)


def test_wait_on_a_job_id_returns_the_collectors_line(
        env, monkeypatch, capsys):
    """A job id is the other name the verb accepts, same line, same code."""
    log = str(env / "worker.log")
    Path(log).write_text("still running\n", encoding="utf-8")
    seat({SID: worker_session(SID, log=log)})
    seed_job(JOB, log=log)
    threading.Thread(
        target=lambda: (time.sleep(0.2),
                        complete(JOB, state="failed", exit_code=3)),
        daemon=True,
    ).start()

    assert cli.main(["wait", JOB]) == 3
    assert capsys.readouterr().out.strip() == expected_line(
        JOB, "failed", "3", log)


def test_a_log_that_already_carries_the_marker_does_not_end_a_wait(
        env, capsys):
    """The waiter never opens the log: a marker there with no terminal
    ledger line is still a wait, and --timeout 1s exits 124."""
    log = env / "echoed.log"
    log.write_text("the prompt quotes the marker\n" + LOG_MARKER,
                   encoding="utf-8")
    seat({SID: worker_session(SID, log=str(log))})
    seed_job(JOB, log=str(log))

    assert cli.main(["wait", JOB, "--timeout", "1s"]) == 124
    err = capsys.readouterr().err
    assert f"{JOB} waiting …" in err


def test_two_ids_print_in_finish_order_and_exit_the_higher_code(
        env, capsys):
    """Argument order is not finish order; the exit status is the higher
    of the two worker codes."""
    log_a = str(env / "a.log")
    log_b = str(env / "b.log")
    seat({SID: worker_session(SID)})
    seed_job(JOB_A, log=log_a, session="ses-a")
    seed_job(JOB_B, log=log_b, session="ses-b")

    def collect() -> None:
        time.sleep(0.15)
        complete(JOB_B, state="failed", exit_code=3)
        time.sleep(0.15)
        complete(JOB_A, state="returned", exit_code=0)

    threading.Thread(target=collect, daemon=True).start()
    assert cli.main(["wait", JOB_A, JOB_B]) == 3
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        expected_line(JOB_B, "failed", "3", log_b),
        expected_line(JOB_A, "returned", "0", log_a),
    ]


def test_wait_prints_timed_out_for_a_timeout_kill(env, capsys):
    """A failed line whose reason is a timeout prints `timed out`, not
    `failed`, and exits 1 when the line carries no code."""
    log = str(env / "worker.log")
    seat({SID: worker_session(SID, log=log)})
    seed_job(JOB, log=log, state="failed", outcome_reason="job timed out",
             exit_code=None)

    assert cli.main(["wait", JOB]) == 1
    assert capsys.readouterr().out.strip() == expected_line(
        JOB, "timed out", "?", log)


def test_unknown_id_is_refused_by_name(env, capsys):
    """A typo is a refusal naming the id, never a wait on a guess."""
    assert cli.main(["wait", "job-nope000"]) == 1
    assert "unknown job or session 'job-nope000'" in capsys.readouterr().err


def test_worker_role_is_refused_by_role(env, monkeypatch, capsys):
    """Workers do not wait on other jobs: the gate holds for wait too."""
    log = str(env / "worker.log")
    seat({WORKER: worker_session(WORKER, job=None, front=None),
          SID: worker_session(SID, log=log)})
    seed_job(JOB, log=log)
    monkeypatch.setenv(SESSION_ENV, WORKER)

    assert cli.main(["wait", JOB]) == 1
    assert "may not call 'wait'" in capsys.readouterr().err


def test_wait_help_says_it_reads_the_ledger_not_the_log(env, capsys):
    """--help names the ledger and says the log is not what it waits on."""
    with pytest.raises(SystemExit) as caught:
        cli.main(["wait", "--help"])
    assert caught.value.code == 0
    text = capsys.readouterr().out.lower()
    assert "ledger" in text
    assert "log" in text


def test_job_file_omits_the_marker_and_the_dry_run_command_keeps_it(
        env, fake_pool, capsys):
    """FOREMAN-JOB.md no longer quotes the marker; the wrapper command
    that writes it still does, and a dry run still prints it."""
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    dry_tree = str(env / "wt-dry")
    assert cli.main(["launch", "muse", "muse", str(spec),
                     "--repo", str(repo), "--worktree", dry_tree,
                     "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "### finished rc=$?" in out
    assert out.index("### finished rc=$?") < out.index("| tee")

    worktree = env / "wt-job"
    assert cli.main(["launch", "muse", "fake", str(spec),
                     "--repo", str(repo), "--worktree", str(worktree)]) == 0
    capsys.readouterr()
    job_text = (worktree / "FOREMAN-JOB.md").read_text(encoding="utf-8")
    assert "### finished" not in job_text
    assert "foreman wait <session>" in job_text
    env_block = launch_module.environment_block(
        "/tmp/wt", "job/x", "main", "/tmp/scratch", "/tmp/log",
        "/tmp/verdict", "20m")
    assert "### finished" not in env_block
    assert "foreman wait <session>" in env_block
