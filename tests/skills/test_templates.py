"""Role prompt templates: every summoned role renders, every field lands.

Each test renders through the launcher's real code path — the same
``render_*`` function the launch shapes call — and asserts the design's
parts are present, the injected values actually appear, and no ``{{``
survives. The break each test catches: a template edit that drops a
section, a mapping that stops supplying a field, or a render that serves
the wrong role's file.
"""

import subprocess
from pathlib import Path

import pytest

from foreman import cli, launch, paths, store

RULING = "TEMPLATE-TEST-RULING: verify by re-running before anything is done."
FRONT = "skills"
SESSION = "ses-tmpl-001"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    store.append_ledger(
        paths.rulings_path(),
        {"scope": "swarm", "text": RULING, "source": "owner"},
    )
    return tmp_path


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


def worker_env_block() -> str:
    return launch.environment_block(
        "/tmp/tmpl-test/wt", "tmpl-branch", "main",
        "/tmp/tmpl-test/scratch", "/tmp/tmpl-test/log",
        "/tmp/tmpl-test/verdict", "20m",
    )


def render_worker(role: str, kind: str | None = None) -> str:
    return launch.render_worker_prompt(
        role=role,
        kind=kind,
        front=FRONT,
        supervisor=FRONT,
        session_id=SESSION,
        verdict_path="/tmp/tmpl-test/verdict",
        rulings=launch.format_rulings(launch.read_rulings(FRONT)),
        environment=worker_env_block(),
    )


# role -> one line only that role's template carries. The break: the
# launcher serving the wrong file for a role renders green but wrong.
WORKER_VOICE = {
    "opus": "You do core-logic engineering with precision",
    "muse": "You do the work that does not need deep reasoning",
    "astra": "## Verdict schema",
    "grok": "with a different model's eyes",
}

WORKER_PARTS = [
    "## Who you are",
    "## Which front",
    "## Who you report to",
    "## What you report and when",
    "## Your goal",
    "## Your tools",
    "## The rulings",
    "## The environment contract",
]


@pytest.mark.parametrize("role", ["opus", "muse", "astra", "grok"])
def test_worker_role_renders_with_every_section_and_value(env, role):
    text = render_worker(role)
    for part in WORKER_PARTS:
        assert part in text, f"{role} prompt lost {part}"
    assert WORKER_VOICE[role] in text
    assert FRONT in text
    assert SESSION in text
    assert "/tmp/tmpl-test/verdict" in text
    assert RULING in text
    assert "/tmp/tmpl-test/wt" in text
    assert "{{" not in text


def test_supervisor_renders_with_every_section_and_value(env, tmp_path):
    role_prompt = tmp_path / "role-prompt.md"
    text = launch.render_supervisor_prompt(
        front=FRONT,
        record={"want": "TEMPLATE-WANT", "done_when": "TEMPLATE-DONE",
                "allocation": {"muse": 2}},
        session_id=SESSION,
        repo=str(tmp_path / "repo"),
        branch="main",
        role_prompt=role_prompt,
        predecessor=None,
    )
    for part in ["## Who you are and which front you own",
                 "## What this front wants",
                 "## Done when",
                 "## The tasks on this front",
                 "## Who you report to, and what you report and when",
                 "## Your allocation",
                 "## Your tools",
                 "## The rulings",
                 "## The environment contract"]:
        assert part in text, f"supervisor prompt lost {part}"
    assert "TEMPLATE-WANT" in text
    assert "TEMPLATE-DONE" in text
    assert "muse: at most 2 at once" in text
    assert "checkpoint" in text
    assert RULING in text
    assert SESSION in text
    assert str(tmp_path / "repo") in text
    assert "FOREMAN_SESSION" in text
    assert str(role_prompt) in text
    assert "{{" not in text


def test_merge_desk_renders_with_every_section_and_value(env, tmp_path):
    role_prompt = tmp_path / "role-prompt.md"
    text = launch.render_merge_desk_prompt(
        session_id=SESSION,
        repo=str(tmp_path / "repo"),
        branch="main",
        role_prompt=role_prompt,
    )
    assert "You are the Foreman merge desk" in text
    assert "You are the only merge desk" in text
    assert "(the merge queue is empty)" in text
    assert "merge take" in text
    assert RULING in text
    assert SESSION in text
    assert str(tmp_path / "repo") in text
    assert "FOREMAN_SESSION" in text
    assert str(role_prompt) in text
    assert "{{" not in text


def test_foreman_renders_with_every_section_and_value(env, tmp_path):
    role_prompt = tmp_path / "FOREMAN-ROLE.md"
    text = launch.render_foreman_prompt(
        session_id=SESSION,
        repo=str(tmp_path / "repo"),
        branch="main",
        role_prompt=role_prompt,
    )
    for part in ["## Who you are",
                 "## The fronts you hold",
                 "## Who you report to, and what you report and when",
                 "## Your goal",
                 "## Your tools",
                 "## The rulings",
                 "## The environment contract"]:
        assert part in text, f"foreman prompt lost {part}"
    assert "You are the Foreman" in text
    assert "the only foreman" in text
    assert "(no fronts on the ledger" in text
    for verb in ("admit", "answer", "digest", "route"):
        assert verb in text, f"foreman prompt lost verb {verb}"
    assert RULING in text
    assert SESSION in text
    assert str(tmp_path / "repo") in text
    assert "FOREMAN_SESSION" in text
    assert "FOREMAN-ROLE.md" in text
    assert str(paths.state_dir()) in text
    assert "{{" not in text


def test_job_file_keeps_section_order_and_values(env):
    text = launch.build_job_file(
        "job-tmpl-7", "implement", "Tmpl Title", FRONT,
        worker_env_block(), "- " + RULING, "TEMPLATE-SCOPE",
        "WHAT: build it.\nRun: pytest tests/skills -q", "3-7",
        role_text="TEMPLATE-ROLE-TEXT",
    )
    assert text.startswith(
        '# Job job-tmpl-7 · implement · task "Tmpl Title" · front skills\n')
    order = ["## Role prompt (from FOREMAN-ROLE.md",
             "## Environment (injected)",
             "## Rules (injected: swarm + front rulings, verbatim)",
             "## Task scope (verbatim from the brief)",
             "## This job (written by the supervisor)"]
    positions = [text.index(section) for section in order]
    assert positions == sorted(positions), "job sections out of order"
    assert "TEMPLATE-ROLE-TEXT" in text
    assert "units: 3-7" in text
    assert "TEMPLATE-SCOPE" in text
    assert RULING in text
    assert "/tmp/tmpl-test/wt" in text
    assert "{{" not in text


def test_missing_field_is_refused_naming_every_hole(env):
    with pytest.raises(launch.Refused) as caught:
        launch.render_role_template("muse", {"role": "muse"})
    message = str(caught.value)
    for field in ("front", "supervisor", "session_id",
                  "verdict_path", "rulings", "environment"):
        assert field in message, f"refusal hides missing {field}"


def test_unknown_mapping_key_is_refused(env):
    with pytest.raises(launch.Refused) as caught:
        launch.render_role_template("muse", {
            "role": "muse",
            "front": FRONT,
            "supervisor": FRONT,
            "session_id": SESSION,
            "verdict_path": "/tmp/tmpl-test/verdict",
            "rulings": "- " + RULING,
            "environment": worker_env_block(),
            "zzz_extra": "dead code",
        })
    assert "zzz_extra" in str(caught.value)


def test_launch_foreman_dry_run_leaves_nothing_behind(env, tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    rc = cli.main(["launch", "foreman", "--workspace", "6",
                     "--repo", str(repo), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "You are the Foreman" in out
    assert "FOREMAN-ROLE.md" in out
    session = next(line.split("session: ", 1)[1].strip()
                   for line in out.splitlines()
                   if line.startswith("session: "))
    assert not paths.session_dir(session).exists()
    from foreman import caller
    assert caller.read_roster().get("sessions", {}) == {}


def test_launch_foreman_refuses_a_second_live_foreman(env, tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    store.write_snapshot(paths.roster_path(), {"sessions": {
        "ses-live-1": {"id": "ses-live-1", "role": "foreman",
                       "state": "running", "pid": None},
    }})
    rc = cli.main(["launch", "foreman", "--repo", str(repo), "--dry-run"])
    assert rc == 1
    assert "already live" in capsys.readouterr().err


# A pool that both builds and reviews needs one template per job kind:
# the prompt is a property of the job, not of which vendor was cheap.
KIND_VOICE = {
    ("grok", "implement"): "you write no verdict",
    ("grok", "review"): "with a different model's eyes",
    ("grok", None): "with a different model's eyes",
}


@pytest.mark.parametrize("role,kind", sorted(
    KIND_VOICE, key=lambda pair: (pair[0], pair[1] or "")))
def test_the_job_kind_chooses_the_worker_template(env, role, kind):
    """An implement job reads as a builder and a review job as a reviewer.

    The break this holds shut: the launcher served one file per pool, so
    a builder launched on a reviewing pool was told to change no source
    file and write a verdict. Two jobs died that way, green and empty.
    """
    text = render_worker(role, kind)
    assert KIND_VOICE[(role, kind)] in text
    for part in WORKER_PARTS:
        assert part in text, f"{role}/{kind} prompt lost {part}"
    assert "{{" not in text


def test_a_kind_with_no_template_of_its_own_falls_back_to_the_role(env):
    """muse names no per-kind template, so every kind reads the same."""
    assert launch.worker_template("muse", "implement") == "muse"
    assert launch.worker_template("muse", "review") == "muse"
    assert launch.worker_template("grok", "implement") == "grok.implement"
    assert launch.worker_template("grok", "review") == "grok"
    assert launch.worker_template("grok", None) == "grok"
    assert render_worker("muse", "review") == render_worker("muse")
