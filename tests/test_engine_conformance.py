"""The contract check passes a conforming engine and names what a flawed one misses."""

from __future__ import annotations

import pytest

from arkagent.engines.client import EngineTimeout
from arkagent.engines.conformance import CHECKS, EXIT_BUSY, _working, main, run_checks
from fake_engine import FakeEngine


def _failed(engine: FakeEngine) -> set[str]:
    results = run_checks(engine.client(), poll=0.2, sleep=engine.sleep, clock=engine.clock)
    assert [result.name for result in results] == [name for name, _ in CHECKS]
    return {result.name for result in results if not result.ok}


def test_conforming_engine_passes_every_check():
    engine = FakeEngine()

    results = run_checks(engine.client(), poll=0.2, sleep=engine.sleep, clock=engine.clock)

    assert [(result.name, result.detail) for result in results if not result.ok] == []
    assert len(results) == len(CHECKS)


# 완료된 selftest 작업이 없으면 승인 기록 검사 둘도 볼 대상이 없어 함께 실패한다.
_SELFTEST = {"selftest_runs", "review_is_recorded", "finding_review_is_recorded"}


@pytest.mark.parametrize(
    ("flaw", "expected"),
    [
        ("no_contract", {"health"}),
        ("no_busy", {"health"} | _SELFTEST),
        ("accepts_bad_recipe", {"rejects_bad_input"}),
        ("no_origin_check", {"rejects_foreign_origin"}),
        ("one_stage", _SELFTEST),
        ("absolute_paths", _SELFTEST),
        ("approves_itself", _SELFTEST),
        ("no_findings", _SELFTEST),
        ("unknown_memory_method", _SELFTEST),
        ("clears_itself", _SELFTEST),
        ("stale_current_job", _SELFTEST),
        ("ignores_cancel", {"cancel_stops_a_running_job"}),
        ("wrong_error_code", {"failure_is_classified"}),
        ("review_unsupported", {"review_is_recorded"}),
        ("finding_review_unsupported", {"finding_review_is_recorded"}),
    ],
)
def test_each_flaw_fails_the_check_that_guards_it(flaw, expected):
    assert _failed(FakeEngine(flaw)) == expected


def test_engine_without_selftest_cannot_be_verified():
    failed = _failed(FakeEngine("no_selftest"))

    assert {"recipes", "selftest_runs", "cancel_stops_a_running_job", "failure_is_classified"} <= failed
    assert "health" not in failed


def test_job_folder_must_be_an_absolute_path():
    failed = _failed(FakeEngine("relative_dir"))

    assert failed == {"selftest_runs", "cancel_stops_a_running_job", "failure_is_classified",
                      "review_is_recorded", "finding_review_is_recorded", "lists_recent_jobs"}


def test_wait_gives_up_on_a_job_that_never_finishes():
    engine = FakeEngine()
    client = engine.client()
    job_id = client.submit("selftest", {"steps": 2, "stepSeconds": 1000.0})["id"]

    with pytest.raises(EngineTimeout):
        client.wait(job_id, timeout=5.0, poll=1.0, sleep=engine.sleep, clock=engine.clock)
    assert client.wait(job_id, until=frozenset({"running"}), timeout=1.0, sleep=engine.sleep,
                       clock=engine.clock)["state"] == "running"


def test_command_reports_every_check_and_fails_when_nothing_answers(capsys):
    status = main(["http://127.0.0.1:9"])  # nothing listens on the discard port

    out = capsys.readouterr().out
    assert status == 1
    assert out.count("FAIL") == len(CHECKS)
    assert f"0/{len(CHECKS)} passed" in out


@pytest.mark.parametrize("work", ["queued job", "outside the queue"])
def test_command_runs_no_check_on_an_engine_that_is_working(work, capsys):
    engine = FakeEngine()
    if work == "queued job":
        engine.client().submit("selftest", {"stepSeconds": 1000.0})
    else:
        engine.outside = "app:15100"

    status = main(["http://127.0.0.1:47999"], client=engine.client())

    out = capsys.readouterr().out
    assert status == EXIT_BUSY
    assert out.startswith("BUSY") and "FAIL" not in out and "PASS" not in out
    assert len(engine.jobs) == (1 if work == "queued job" else 0)


def test_engine_that_only_holds_a_resident_model_is_checked():
    engine = FakeEngine()
    engine.resident = "ace-step:18001"  # 앱이 켜 둔 상주 모델 서버 — selftest는 그것을 쓰지 않는다

    assert _working(engine.client()) is None
    results = run_checks(engine.client(), poll=0.2, sleep=engine.sleep, clock=engine.clock)
    assert [(result.name, result.detail) for result in results if not result.ok] == []


def test_resident_model_must_be_declared_to_be_checked(capsys):
    engine = FakeEngine("hides_resident")
    engine.resident = "ace-step:18001"

    assert main(["http://127.0.0.1:47999"], client=engine.client()) == EXIT_BUSY
