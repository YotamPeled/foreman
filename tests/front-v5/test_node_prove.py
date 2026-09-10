"""node prove records red on the base and green on the head.

Every test drives the real CLI against a fresh FOREMAN_STATE with a
v5-shape front and a fixture repository whose head adds the file the
verify ``test -f proof.txt && echo seen`` needs.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, paths, store
from foreman.caller import SESSION_ENV

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

PROOF_VERIFY = "test -f proof.txt && echo seen"


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


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_bare(root: Path) -> Path:
    src = root / "src-repo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    bare = root / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(bare)],
                   check=True)
    return bare


def make_proof_repo(root: Path) -> tuple[Path, str, str]:
    """A clone whose head adds ``proof.txt``; the parent commit does not."""
    repo = root / "proof-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "foreman-test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "proof.txt").write_text("proof\n", encoding="utf-8")
    (repo / "notes.txt").write_text("# note\n", encoding="utf-8")
    git(repo, "add", "proof.txt", "notes.txt")
    git(repo, "commit", "-qm", "head")
    head = git(repo, "rev-parse", "HEAD")
    return repo, base, head


def write_v5(root: Path, name: str, url: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url), encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape") -> None:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    bare = make_bare(env)
    assert cli.main(["front", "add", str(write_v5(env, name, str(bare)))]) == 0
    capsys.readouterr()


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


def seed_supervisor(front, sid=SUP):
    store.write_snapshot(
        paths.roster_path(),
        {"sessions": {sid: session_entry(sid, "supervisor", front)}})


def add_argv(front="v5shape", parent="v5shape", kind="milestone",
             title="Front inputs", **flags):
    argv = ["node", "add", front, "--parent", parent, "--kind", kind,
            "--title", title,
            "--verify", flags.get("verify", PROOF_VERIFY),
            "--must-not-touch", flags.get("must_not_touch",
                                          "the live state directory"),
            "--reason", flags.get("reason", "the tree needs this node"),
            "--break", flags.get("break_", "accept a green base"),
            "--repo", flags.get("repo", "foreman")]
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    if flags.get("break_patch") is not None:
        argv.extend(["--break-patch", flags["break_patch"]])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def folded_node(front: str, nid: str) -> dict:
    folded = store.fold_by_id(store.read_ledger(paths.front_tree_path(front)))
    return {line["id"]: line for line in folded}[nid]


def add_task(monkeypatch, capsys, verify=PROOF_VERIFY, node_id="nod-prove1",
             break_patch=None):
    mil = add_ok(monkeypatch, capsys, title="milestone one")
    return add_ok(monkeypatch, capsys, parent=mil, kind="task",
                  title="the proof", verify=verify, node_id=node_id,
                  break_patch=break_patch)


def worktree_diff(repo: Path, mutate) -> str:
    """Unstaged unified diff of ``mutate`` against the current HEAD."""
    mutate(repo)
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.returncode == 0, proc.stderr
    git(repo, "checkout", "--", ".")
    subprocess.run(
        ["git", "-C", str(repo), "clean", "-fdq"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    assert proc.stdout.strip(), "mutate produced an empty diff"
    return proc.stdout


def test_node_prove_records_base_red_head_green_proven(
        env, monkeypatch, capsys):
    """Base without proof.txt is red; head with it is green: proven."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    repo, base, head = make_proof_repo(env)
    nid = add_task(monkeypatch, capsys)
    assert run(monkeypatch, [
        "node", "prove", "v5shape", nid,
        "--base", base, "--head", head, "--repo", str(repo),
    ], SUP) == 0
    out, err = capsys.readouterr()
    assert "proven" in out
    assert "no break_patch" in err
    assert "prose by hand" in err
    record = folded_node("v5shape", nid)
    prove = record["prove"]
    assert prove["base"]["saw"] == "red"
    assert prove["head"]["saw"] == "green"
    assert prove["verdict"] == "proven"
    assert prove["break"] == {"verdict": "prose only"}
    assert prove["at"]
    assert prove["base"]["exit"] != 0
    assert prove["head"]["exit"] == 0
    for side in ("base", "head"):
        dest = Path(prove[side]["output_ref"])
        assert dest.is_file()
        assert dest.parent == paths.session_dir(SUP) / "prove"
        assert isinstance(prove[side]["seconds"], int)
    assert "seen" in Path(prove["head"]["output_ref"]).read_text(
        encoding="utf-8")
    assert not paths.scratch_worktree_dir("prove", f"{nid}-base").exists()
    assert not paths.scratch_worktree_dir("prove", f"{nid}-head").exists()
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "rev-parse", base) == base
    assert git(repo, "rev-parse", head) == head


def test_node_prove_same_ref_is_unproven_naming_base_green(
        env, monkeypatch, capsys):
    """--base equal to --head on the green commit is unproven, naming green."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    repo, _base, head = make_proof_repo(env)
    nid = add_task(monkeypatch, capsys, node_id="nod-same1")
    assert run(monkeypatch, [
        "node", "prove", "v5shape", nid,
        "--base", head, "--head", head, "--repo", str(repo),
    ], SUP) == 1
    capsys.readouterr()
    prove = folded_node("v5shape", nid)["prove"]
    assert prove["base"]["saw"] == "green"
    assert prove["head"]["saw"] == "green"
    assert prove["verdict"].startswith("unproven")
    assert "base: green" in prove["verdict"]


def test_node_prove_silent_true_is_unproven(env, monkeypatch, capsys):
    """A verify of ``true`` records silent on both sides and is unproven."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    repo, base, head = make_proof_repo(env)
    nid = add_task(monkeypatch, capsys, verify="true", node_id="nod-true1")
    assert run(monkeypatch, [
        "node", "prove", "v5shape", nid,
        "--base", base, "--head", head, "--repo", str(repo),
    ], SUP) == 1
    capsys.readouterr()
    prove = folded_node("v5shape", nid)["prove"]
    assert prove["base"]["saw"] == "silent"
    assert prove["head"]["saw"] == "silent"
    assert prove["verdict"].startswith("unproven")
    assert "silent" in prove["verdict"]


def test_node_prove_missing_program_is_silent_unproven(
        env, monkeypatch, capsys):
    """A verify naming a program that does not resolve records silent."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    repo, base, head = make_proof_repo(env)
    nid = add_task(monkeypatch, capsys,
                   verify="foreman-no-such-verify-cmd",
                   node_id="nod-miss1")
    assert run(monkeypatch, [
        "node", "prove", "v5shape", nid,
        "--base", base, "--head", head, "--repo", str(repo),
    ], SUP) == 1
    capsys.readouterr()
    prove = folded_node("v5shape", nid)["prove"]
    assert prove["base"]["saw"] == "silent"
    assert prove["head"]["saw"] == "silent"
    assert prove["verdict"].startswith("unproven")
    log = Path(prove["base"]["output_ref"]).read_text(encoding="utf-8")
    assert "resolve" in log
    assert not paths.scratch_worktree_dir("prove", f"{nid}-base").exists()
    assert not paths.scratch_worktree_dir("prove", f"{nid}-head").exists()


def test_node_prove_break_patch_deleting_proof_is_red(
        env, monkeypatch, capsys):
    """A patch that deletes proof.txt makes the break red and exits 0."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    repo, base, head = make_proof_repo(env)
    patch = worktree_diff(repo, lambda r: (r / "proof.txt").unlink())
    patch_file = env / "delete.patch"
    patch_file.write_text(patch, encoding="utf-8")
    nid = add_task(monkeypatch, capsys, node_id="nod-del1",
                   break_patch=str(patch_file))
    stored = folded_node("v5shape", nid)
    assert stored["break_patch"] == patch
    assert run(monkeypatch, [
        "node", "prove", "v5shape", nid,
        "--base", base, "--head", head, "--repo", str(repo),
    ], SUP) == 0
    capsys.readouterr()
    prove = folded_node("v5shape", nid)["prove"]
    assert prove["verdict"] == "proven"
    assert prove["break"]["saw"] == "red"
    assert prove["break"]["verdict"] == "red"
    assert not paths.scratch_worktree_dir("prove", f"{nid}-base").exists()
    assert not paths.scratch_worktree_dir("prove", f"{nid}-head").exists()
    assert not paths.scratch_worktree_dir("prove", f"{nid}-break").exists()
    assert git(repo, "rev-parse", base) == base
    assert git(repo, "rev-parse", head) == head
    assert (repo / "proof.txt").is_file()


def test_node_prove_comment_only_patch_survives_exits_3(
        env, monkeypatch, capsys):
    """A patch that only changes a comment survives: classify line, exit 3."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    repo, base, head = make_proof_repo(env)

    def change_comment(path: Path) -> None:
        (path / "notes.txt").write_text("# other\n", encoding="utf-8")

    patch = worktree_diff(repo, change_comment)
    patch_file = env / "comment.patch"
    patch_file.write_text(patch, encoding="utf-8")
    nid = add_task(monkeypatch, capsys, node_id="nod-cmt1",
                   break_patch=str(patch_file))
    assert run(monkeypatch, [
        "node", "prove", "v5shape", nid,
        "--base", base, "--head", head, "--repo", str(repo),
    ], SUP) == 3
    out, _err = capsys.readouterr()
    assert "BREAK SURVIVED:" in out
    assert "node revise --break-class gap|ineffective|vacuous --reason" in out
    prove = folded_node("v5shape", nid)["prove"]
    assert prove["verdict"] == "proven"
    assert prove["break"]["verdict"] == "survived"
    assert not paths.scratch_worktree_dir("prove", f"{nid}-break").exists()
    assert git(repo, "rev-parse", base) == base
    assert git(repo, "rev-parse", head) == head
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "# note\n"
