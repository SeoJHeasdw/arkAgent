"""Delegated-job contract — what crosses between the frontier model and arkagent.

The frontier hands down a `Brief`, watches, and may step in. arkagent runs the job
with a local model and hands back a short `Report`. Three rules are enforced here
rather than left to convention:

  - One owner at a time. Only the current owner may advance a job, so the local
    agent and the frontier never work the same job at once.
  - Reports are short. The frontier pays for every token it reads, so a report is
    a capped summary plus paths, never the content itself.
  - Every takeover records why. The cause is the evidence for what would have to
    change (hardware, model, or an engine) for the local side to finish alone.

Everything is a frozen value and transitions return a new `Job`. Persistence, the
queue, and the transport that exposes this to the frontier live elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from ..core.errors import ConflictError, ValidationError
from ..core.types import new_id, now_ms

SUMMARY_MAX_CHARS = 600


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"  # the local side asked for help and is not advancing
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})


class Owner(str, Enum):
    LOCAL = "local"
    FRONTIER = "frontier"


class TakeoverCause(str, Enum):
    """Why the frontier did the work itself. The first two are fixed by better
    hardware, the third only by a more capable model, the fourth by neither."""

    SLOW = "slow"
    MEMORY = "memory"
    WRONG_JUDGMENT = "wrong_judgment"
    ENGINE_FAULT = "engine_fault"
    OTHER = "other"


class TakeoverTrigger(str, Enum):
    ESCALATED = "escalated"  # the local side asked
    PREEMPTED = "preempted"  # the frontier stepped in unasked


@dataclass(frozen=True)
class Brief:
    """What the frontier asks for."""

    instruction: str
    recipe: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewItem:
    """One spot a person should look at or listen to."""

    where: str
    why: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    summary: str
    artifacts: tuple[str, ...] = ()
    review_items: tuple[ReviewItem, ...] = ()

    def __post_init__(self) -> None:
        if len(self.summary) > SUMMARY_MAX_CHARS:
            raise ValidationError(
                f"report summary is {len(self.summary)} chars (max {SUMMARY_MAX_CHARS})",
                code="job.report_too_long",
                params={"length": len(self.summary), "max": SUMMARY_MAX_CHARS},
            )


@dataclass(frozen=True)
class Takeover:
    cause: TakeoverCause
    trigger: TakeoverTrigger
    note: str = ""
    tried: tuple[str, ...] = ()
    next_step: str | None = None
    at_ms: int = field(default_factory=now_ms)


@dataclass(frozen=True)
class Metrics:
    machine: str = ""
    queue_seconds: float = 0.0
    run_seconds: float = 0.0
    model_load_seconds: float = 0.0
    peak_memory_bytes: int = 0
    local_input_tokens: int = 0
    local_output_tokens: int = 0
    # arkagent는 프론티어의 사용량을 볼 수 없다 — 작업이 끝난 뒤 밖에서 채운다.
    frontier_tokens: int | None = None


@dataclass(frozen=True)
class Job:
    brief: Brief
    id: str = field(default_factory=lambda: new_id("job"))
    state: JobState = JobState.QUEUED
    owner: Owner = Owner.LOCAL
    # 작업 상태가 놓인 곳. 넘겨받은 쪽이 처음부터가 아니라 중간부터 잇기 위한 것.
    state_dir: str | None = None
    report: Report | None = None
    error: str | None = None
    takeovers: tuple[Takeover, ...] = ()
    metrics: Metrics = field(default_factory=Metrics)
    created_ms: int = field(default_factory=now_ms)


def _require_open(job: Job) -> None:
    if job.state in TERMINAL_STATES:
        raise ConflictError(f"job {job.id} is already {job.state.value}", code="job.closed")


def require_owner(job: Job, actor: Owner) -> None:
    """Raise unless `actor` may advance `job`. The local executor calls this at
    every checkpoint so a takeover stops it at the next step."""
    _require_open(job)
    if job.owner is not actor:
        raise ConflictError(
            f"job {job.id} is owned by {job.owner.value}",
            code="job.not_owner",
            params={"owner": job.owner.value},
        )


def start(job: Job) -> Job:
    if job.state is not JobState.QUEUED:
        raise ConflictError(f"job {job.id} is {job.state.value}, not queued", code="job.not_queued")
    return replace(job, state=JobState.RUNNING)


def escalate(
    job: Job,
    cause: TakeoverCause,
    *,
    note: str = "",
    tried: tuple[str, ...] = (),
    next_step: str | None = None,
) -> Job:
    """The local side stops and asks. Ownership stays local until the frontier
    either takes the job or answers and lets it resume."""
    require_owner(job, Owner.LOCAL)
    if job.state is not JobState.RUNNING:
        raise ConflictError(f"job {job.id} is {job.state.value}, not running", code="job.not_running")
    asked = Takeover(cause=cause, trigger=TakeoverTrigger.ESCALATED, note=note, tried=tried, next_step=next_step)
    return replace(job, state=JobState.WAITING, takeovers=(*job.takeovers, asked))


def resume(job: Job) -> Job:
    """The frontier answered an escalation without taking the job."""
    require_owner(job, Owner.LOCAL)
    if job.state is not JobState.WAITING:
        raise ConflictError(f"job {job.id} is {job.state.value}, not waiting", code="job.not_waiting")
    return replace(job, state=JobState.RUNNING)


def take_over(job: Job, cause: TakeoverCause | None = None, *, note: str = "") -> Job:
    """The frontier takes the job. Answering an escalation needs no cause — the
    escalation already recorded one. Stepping in unasked must say why."""
    _require_open(job)
    if job.owner is Owner.FRONTIER:
        raise ConflictError(f"job {job.id} is already owned by the frontier", code="job.already_taken")
    if job.state is JobState.WAITING:
        return replace(job, owner=Owner.FRONTIER, state=JobState.RUNNING)
    if cause is None:
        raise ValidationError("taking over a job that did not escalate needs a cause", code="job.cause_required")
    stepped_in = Takeover(cause=cause, trigger=TakeoverTrigger.PREEMPTED, note=note)
    return replace(
        job,
        owner=Owner.FRONTIER,
        state=JobState.RUNNING,
        takeovers=(*job.takeovers, stepped_in),
    )


def hand_back(job: Job) -> Job:
    """The frontier unblocked the job and returns it to the local side."""
    require_owner(job, Owner.FRONTIER)
    return replace(job, owner=Owner.LOCAL, state=JobState.RUNNING)


def complete(job: Job, report: Report, *, by: Owner) -> Job:
    require_owner(job, by)
    if job.state is not JobState.RUNNING:
        raise ConflictError(f"job {job.id} is {job.state.value}, not running", code="job.not_running")
    return replace(job, state=JobState.DONE, report=report)


def fail(job: Job, error: str, *, by: Owner) -> Job:
    require_owner(job, by)
    return replace(job, state=JobState.FAILED, error=error)


def cancel(job: Job) -> Job:
    _require_open(job)
    return replace(job, state=JobState.CANCELLED)
