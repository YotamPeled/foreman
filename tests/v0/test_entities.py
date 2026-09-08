"""Every entity round-trips through to_dict / from_dict / JSON."""

import json

import pytest

from foreman import entities
from foreman.entities import (
    Allocation,
    Anomaly,
    Checkpoint,
    Component,
    Digest,
    Event,
    Evidence,
    Finding,
    InboxItem,
    Job,
    Measurement,
    Merge,
    Pool,
    Ruling,
    Session,
    SlotGrant,
    Task,
)

INSTANCES = [
    Component(
        id="cmp-abc1234",
        name="corpus",
        order=2,
        prefer=1,
        after=["manifest"],
        want="read apps",
        done_when="labels agree",
        land_on="dev",
        reviews="on request",
        allocation={"muse": 5, "opus": 1},
        supervisor="ses-xyz",
        brief_path="/c/corpus/brief.toml",
        state="active",
    ),
    Task(
        id="tas-abc1234",
        component="cmp-abc1234",
        title="second reads",
        scope="WHAT: x\nINPUTS: y\nOUTPUTS: z\nOUT OF SCOPE: w",
        verify="foreman-verify reads",
        size=600,
        after=["manifest"],
        timeout="20m",
        land_on="dev",
        state="active",
        units_done=12,
        units_total=600,
    ),
    Job(
        id="job-abc1234",
        task="tas-abc1234",
        kind="implement",
        role="muse",
        priority=3,
        spec_path="/w/FOREMAN-JOB.md",
        session="ses-abc1234",
        worktree="/w",
        branch="job/x",
        log="/w/log",
        timeout="20m",
        units=[1, 5],
        attempt=2,
        state="running",
        planned_at="2026-09-08T00:00:00+00:00",
        queued_at="2026-09-08T00:01:00+00:00",
        started_at="2026-09-08T00:02:00+00:00",
        returned_at=None,
        verified_at=None,
        artifact="labels/",
        verdict_path="",
    ),
    Merge(
        id="mrg-abc1234",
        branch="job/x",
        tasks=["tas-abc1234"],
        target="dev",
        review_refs=["rev-1"],
        head="deadbee",
        result="landed",
        requested_at="2026-09-08T00:00:00+00:00",
        landed_at="2026-09-08T01:00:00+00:00",
        by="ses-desk",
    ),
    Session(
        id="ses-abc1234",
        role="supervisor",
        pool="opus",
        model="opus",
        component="cmp-abc1234",
        job=None,
        pid=1234,
        launched_by="ses-foreman",
        started_at="2026-09-08T00:00:00+00:00",
        last_declared_at="2026-09-08T00:05:00+00:00",
        last_observed_at="2026-09-08T00:06:00+00:00",
        cpu_s=12.5,
        state="running",
    ),
    Pool(
        name="muse",
        model="muse",
        adapter="launch",
        slots_total=10,
        kinds=["implement", "research"],
        cannot_take=[],
        timeout_default="20m",
        meter=12.0,
        skill="skills/muse.md",
    ),
    Allocation(component="cmp-abc1234", role="muse", count=5),
    SlotGrant(
        pool="muse",
        component="cmp-abc1234",
        role="muse",
        job="job-abc1234",
        session="ses-abc1234",
        granted_at="2026-09-08T00:00:00+00:00",
        released_at=None,
    ),
    Ruling(
        id="rul-abc1234",
        scope="component",
        text="Nothing goes to Grok.",
        source="owner",
        acks=["ses-abc1234"],
    ),
    Evidence(
        on="tas-abc1234",
        claim="verify passes",
        status="CONFIRMED",
        command="foreman-verify reads",
        output_ref="/w/out.log",
    ),
    Finding(
        on="tas-abc1234",
        class_="scope",
        title="scope crept",
        detail="brief assumed X",
        evidence_ref="evi-1",
    ),
    Measurement(
        monitor="apps",
        value=512.0,
        of=600.0,
        status="ok",
        command="corpus-count apps",
        output_ref="/w/m.log",
    ),
    Checkpoint(
        session="ses-abc1234",
        doing="splitting task",
        next="dispatch jobs",
        held=["tas-abc1234"],
        questions=["q1"],
        jobs_running=["job-abc1234"],
        last_event="2026-09-08T00:06:00+00:00",
    ),
    InboxItem(
        id="inb-abc1234",
        from_="foreman",
        kind="money",
        question="spend $5?",
        recommendation="yes",
        options=["yes", "no"],
        asked_at="2026-09-08T00:00:00+00:00",
        answered_at=None,
        answer=None,
        answered_by=None,
        relayed=False,
    ),
    Anomaly(
        kind="job stalled",
        subject="job-abc1234",
        since="2026-09-08T00:00:00+00:00",
        detail="no mtime for 10m",
        resolved_at=None,
    ),
    Event(
        at="2026-09-08T00:00:00+00:00",
        kind="job returned",
        subject="job-abc1234",
        data={"cpu_s": 3.0},
    ),
    Digest(
        at="2026-09-08T01:00:00+00:00",
        text="steady",
        computed={"jobs_per_h": 4.0},
    ),
]


@pytest.mark.parametrize("instance", INSTANCES, ids=lambda e: type(e).__name__)
def test_entity_round_trips(instance):
    data = instance.to_dict()
    json.dumps(data)
    assert type(instance).from_dict(json.loads(json.dumps(data))) == instance


def test_wire_names_for_keywords():
    assert Finding(on="t", class_="c", title="", detail="", evidence_ref="").to_dict()[
        "class"
    ] == "c"
    assert (
        InboxItem(id="i", from_="f", kind="k", question="q").to_dict()["from"] == "f"
    )
    assert Finding.from_dict({"on": "t", "class": "c"}).class_ == "c"
    assert InboxItem.from_dict({"from": "f"}).from_ == "f"


def test_from_dict_ignores_unknown_keys():
    component = Component.from_dict(
        {"name": "corpus", "state": "active", "future_field": [1, 2], "nonsense": {}}
    )
    assert (component.name, component.state) == ("corpus", "active")
    assert Job.from_dict({"id": "job-x", "bogus": 1}).id == "job-x"
    for cls in (
        Task,
        Merge,
        Session,
        Pool,
        Allocation,
        SlotGrant,
        Ruling,
        Evidence,
        Measurement,
        Checkpoint,
        Anomaly,
        Event,
        Digest,
    ):
        assert type(cls.from_dict({"unknown_key": True})) is cls


def test_state_tuples_cover_listed_states():
    assert set(entities.COMPONENT_STATES) == {"queued", "active", "done", "halted", "frozen"}
    assert set(entities.TASK_STATES) == {"waiting", "ready", "active", "built", "landed"}
    assert set(entities.JOB_STATES) == {
        "planned",
        "queued",
        "running",
        "returned",
        "verified",
        "failed",
        "killed",
    }
    assert set(entities.MERGE_STATES) == {"requested", "merging", "landed", "failed"}
    assert set(entities.SESSION_STATES) == {"running", "exited", "stalled", "killed"}
