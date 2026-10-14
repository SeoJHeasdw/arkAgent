"""Delegated-job contract: ownership, short reports, and takeover records."""

from __future__ import annotations

import pytest

from arkagent.core.errors import ConflictError, ValidationError
from arkagent.jobs.contract import (
    SUMMARY_MAX_CHARS,
    Brief,
    Job,
    JobState,
    Owner,
    Report,
    ReviewItem,
    TakeoverCause,
    TakeoverTrigger,
    cancel,
    complete,
    escalate,
    fail,
    hand_back,
    require_owner,
    resume,
    start,
    take_over,
)


def _running() -> Job:
    return start(Job(brief=Brief(instruction="make the ch01 narration")))


def test_new_job_is_queued_and_owned_by_the_local_side():
    job = Job(brief=Brief(instruction="x"))

    assert job.state is JobState.QUEUED
    assert job.owner is Owner.LOCAL
    assert job.takeovers == ()


def test_start_runs_a_queued_job_once():
    job = _running()

    assert job.state is JobState.RUNNING
    with pytest.raises(ConflictError) as err:
        start(job)
    assert err.value.code == "job.not_queued"


def test_report_summary_is_capped():
    Report(summary="가" * SUMMARY_MAX_CHARS)

    with pytest.raises(ValidationError) as err:
        Report(summary="가" * (SUMMARY_MAX_CHARS + 1))
    assert err.value.code == "job.report_too_long"


def test_escalation_parks_the_job_and_records_what_was_tried():
    job = escalate(
        _running(),
        TakeoverCause.WRONG_JUDGMENT,
        note="reading for 'standalone' keeps failing",
        tried=("스탠드얼론", "스탠드어론"),
        next_step="regenerate page 7",
    )

    assert job.state is JobState.WAITING
    assert job.owner is Owner.LOCAL
    (asked,) = job.takeovers
    assert asked.trigger is TakeoverTrigger.ESCALATED
    assert asked.cause is TakeoverCause.WRONG_JUDGMENT
    assert asked.tried == ("스탠드얼론", "스탠드어론")
    assert asked.next_step == "regenerate page 7"


def test_frontier_answers_an_escalation_without_a_second_record():
    waiting = escalate(_running(), TakeoverCause.SLOW)

    taken = take_over(waiting)

    assert taken.owner is Owner.FRONTIER
    assert taken.state is JobState.RUNNING
    assert len(taken.takeovers) == 1


def test_frontier_stepping_in_unasked_must_say_why():
    job = _running()

    with pytest.raises(ValidationError) as err:
        take_over(job)
    assert err.value.code == "job.cause_required"

    taken = take_over(job, TakeoverCause.MEMORY, note="engine and model cannot be loaded together")
    (stepped_in,) = taken.takeovers
    assert stepped_in.trigger is TakeoverTrigger.PREEMPTED
    assert stepped_in.cause is TakeoverCause.MEMORY
    assert taken.owner is Owner.FRONTIER


def test_local_side_is_locked_out_after_a_takeover():
    taken = take_over(_running(), TakeoverCause.SLOW)

    with pytest.raises(ConflictError) as err:
        require_owner(taken, Owner.LOCAL)
    assert err.value.code == "job.not_owner"
    with pytest.raises(ConflictError):
        complete(taken, Report(summary="done"), by=Owner.LOCAL)
    with pytest.raises(ConflictError):
        take_over(taken, TakeoverCause.SLOW)


def test_frontier_finishes_a_job_it_took():
    taken = take_over(_running(), TakeoverCause.ENGINE_FAULT)
    report = Report(
        summary="narration built; 2 spots to hear",
        artifacts=("output/tts/ch01/track.wav",),
        review_items=(ReviewItem(where="ch01 page 7, 03:46", why="'standalone' reading"),),
    )

    done = complete(taken, report, by=Owner.FRONTIER)

    assert done.state is JobState.DONE
    assert done.report is report


def test_hand_back_returns_the_job_to_the_local_side():
    taken = take_over(_running(), TakeoverCause.OTHER)

    returned = hand_back(taken)

    assert returned.owner is Owner.LOCAL
    assert returned.state is JobState.RUNNING
    require_owner(returned, Owner.LOCAL)
    with pytest.raises(ConflictError):
        hand_back(returned)


def test_resume_continues_locally_after_an_answered_escalation():
    waiting = escalate(_running(), TakeoverCause.WRONG_JUDGMENT)

    resumed = resume(waiting)

    assert resumed.state is JobState.RUNNING
    assert resumed.owner is Owner.LOCAL
    with pytest.raises(ConflictError) as err:
        resume(resumed)
    assert err.value.code == "job.not_waiting"


def test_only_a_running_job_can_escalate_or_complete():
    queued = Job(brief=Brief(instruction="x"))

    with pytest.raises(ConflictError):
        escalate(queued, TakeoverCause.SLOW)
    with pytest.raises(ConflictError):
        complete(queued, Report(summary="done"), by=Owner.LOCAL)


def test_closed_jobs_reject_every_transition():
    closed = (
        complete(_running(), Report(summary="done"), by=Owner.LOCAL),
        fail(_running(), "engine exited 1", by=Owner.LOCAL),
        cancel(_running()),
    )

    for job in closed:
        with pytest.raises(ConflictError) as err:
            cancel(job)
        assert err.value.code == "job.closed"
        with pytest.raises(ConflictError):
            take_over(job, TakeoverCause.SLOW)
        with pytest.raises(ConflictError):
            require_owner(job, job.owner)


def test_cancel_closes_a_job_in_any_open_state():
    queued = Job(brief=Brief(instruction="x"))
    waiting = escalate(_running(), TakeoverCause.SLOW)

    assert cancel(queued).state is JobState.CANCELLED
    assert cancel(waiting).state is JobState.CANCELLED
    assert fail(_running(), "boom", by=Owner.LOCAL).error == "boom"
