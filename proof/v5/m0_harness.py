"""Clause 0.1: the world exists and the CLI answers version."""

from pathlib import Path

CLAUSE = ("0.1", "harness self-check")


def run(world) -> str:
    if not world:
        raise Failure("world is empty")
    for key in ("repo_a", "repo_b", "specs"):
        value = world.get(key)
        if value is None:
            raise Failure(f"world is missing {key}")
        if not Path(value).exists():
            raise Failure(f"world {key} does not exist")
    assert_world()
    proc = run_foreman(["version"])
    if proc.returncode != 0:
        raise Failure(
            f"run_foreman(['version']) exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[-400:]}")
    root = Path(world["repo_a"]).resolve().parent
    return f"world exists under {root} and version exited 0"
