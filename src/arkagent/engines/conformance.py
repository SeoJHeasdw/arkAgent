"""Engine contract check — is a running engine what docs/ENGINE-CONTRACT.md says?

It drives only the required `selftest` recipe, so it never starts model work:

    uv run --project <path to arkAgent> ark-engine-check http://127.0.0.1:47832

Every check runs even when an earlier one fails, so one pass shows everything
that is still missing. Exit status is 0 only when all checks pass, and 3 when the
engine was working so no check ran.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from .client import (
    CONTRACT_VERSION,
    FINAL_STATES,
    FINDING_REVIEW_STATES,
    JOB_STATES,
    REVIEW_STATES,
    EngineClient,
    EngineError,
    is_busy,
)

FOREIGN_ORIGIN = "http://example.invalid"
ERROR_CODES = frozenset({"invalid_input", "out_of_memory", "interrupted", "engine_error"})
SHA256 = re.compile(r"[0-9a-f]{64}")
MEMORY_METHODS = frozenset({"time-l", "sampled"})
EXIT_BUSY = 3


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


class _Failed(Exception):
    pass


def _expect(condition: object, detail: str) -> None:
    if not condition:
        raise _Failed(detail)


def _expect_status(call: Callable[[], Any], status: int, what: str) -> None:
    try:
        call()
    except EngineError as error:
        _expect(error.status == status, f"{what}: answered {error.status}, expected {status}")
        return
    raise _Failed(f"{what}: was accepted, expected {status}")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_relative(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    parts = PurePosixPath(path)
    return not parts.is_absolute() and ".." not in parts.parts


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _check_record(job: dict[str, Any]) -> None:
    """Fields every job record carries, whatever its state."""
    for key in ("id", "recipe", "title"):
        _expect(isinstance(job.get(key), str) and job[key], f"job.{key} must be a non-empty string")
    _expect(isinstance(job.get("dir"), str) and PurePosixPath(job["dir"]).is_absolute(),
            "job.dir must be the absolute path of the job folder")
    _expect(isinstance(job.get("params"), dict), "job.params must be an object")
    _expect(job.get("state") in JOB_STATES, f"job.state is {job.get('state')!r}")
    _expect(_is_timestamp(job.get("createdAt")), "job.createdAt must be an ISO timestamp")
    for key in ("stages", "assets", "findings", "logTail"):
        _expect(isinstance(job.get(key), list), f"job.{key} must be a list")
    for key in ("startedAt", "finishedAt", "error", "errorCode"):
        _expect(key in job, f"job.{key} is missing")


def _check_done_record(job: dict[str, Any]) -> None:
    _check_record(job)
    _expect(job["state"] == "done", f"selftest ended {job['state']!r}: {job.get('error')}")
    _expect(_is_timestamp(job["startedAt"]) and _is_timestamp(job["finishedAt"]), "startedAt/finishedAt must be set")
    _expect(job["error"] is None and job["errorCode"] is None, "a done job has no error or errorCode")

    _expect(len(job["stages"]) >= 2, f"selftest must run 2 stages, ran {len(job['stages'])}")
    for stage in job["stages"]:
        name = stage.get("name")
        _expect(isinstance(name, str) and isinstance(stage.get("label"), str), "stage needs name and label")
        _expect(stage.get("state") == "done", f"stage {name} is {stage.get('state')!r}")
        _expect(_is_number(stage.get("progress")) and stage["progress"] >= 0.999, f"stage {name} progress is not 1.0")
        _expect(_is_number(stage.get("seconds")) and stage["seconds"] >= 0, f"stage {name} has no seconds")
        _expect("peakMemoryBytes" in stage, f"stage {name} has no peakMemoryBytes")
        peak = stage["peakMemoryBytes"]
        _expect(peak is None or (_is_number(peak) and peak >= 0), f"stage {name} peakMemoryBytes is {peak!r}")
        method = stage.get("peakMemoryMethod")
        _expect(method is None or method in MEMORY_METHODS,
                f"stage {name} peakMemoryMethod is {method!r}, expected one of {sorted(MEMORY_METHODS)}")

    _expect(job["assets"], "selftest must leave one asset")
    for asset in job["assets"]:
        for key in ("id", "kind", "role"):
            _expect(isinstance(asset.get(key), str) and asset[key], f"asset.{key} must be a non-empty string")
        _expect(_is_relative(asset.get("file")), f"asset.file must be relative to the job folder: {asset.get('file')!r}")
        _expect(asset.get("review") == "pending", f"a new asset starts pending, not {asset.get('review')!r}")
        sha = asset.get("reviewedSha256")
        _expect(sha is None or (isinstance(sha, str) and SHA256.fullmatch(sha)),
                f"asset.reviewedSha256 must be 64 lowercase hex digits or null: {sha!r}")

    _expect(job["findings"], "selftest must leave one finding")
    for finding in job["findings"]:
        for key in ("id", "code", "where"):
            _expect(isinstance(finding.get(key), str) and finding[key], f"finding.{key} must be a non-empty string")
        _expect(finding.get("severity") in ("fail", "warn"), f"finding.severity is {finding.get('severity')!r}")
        _expect(isinstance(finding.get("params"), dict), "finding.params must be an object")
        evidence = finding.get("evidence")
        _expect(isinstance(evidence, list) and all(_is_relative(item) for item in evidence),
                "finding.evidence must be a list of paths relative to the job folder")
        _expect(finding.get("review") == "pending", f"a new finding starts pending, not {finding.get('review')!r}")


@dataclass
class _Run:
    client: EngineClient
    poll: float
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    done_job: dict[str, Any] | None = field(default=None)

    def wait(self, job_id: str, *, until: frozenset[str] = FINAL_STATES, timeout: float,
             on_poll: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        return self.client.wait(job_id, until=until, timeout=timeout, poll=self.poll,
                                sleep=self.sleep, clock=self.clock, on_poll=on_poll)


def _health(run: _Run) -> None:
    health = run.client.health()
    _expect(health.get("ok") is True, "health.ok must be true")
    _expect(isinstance(health.get("engine"), str) and health["engine"], "health.engine must name the engine")
    _expect(isinstance(health.get("version"), str), "health.version must be a string")
    _expect(health.get("contract") == CONTRACT_VERSION,
            f"health.contract is {health.get('contract')!r}, expected {CONTRACT_VERSION}")
    _expect("currentJob" in health, "health.currentJob is missing")
    _expect(isinstance(health.get("busy"), bool), "health.busy must be a boolean")
    _expect(health.get("busyBy") is None or isinstance(health["busyBy"], str), "health.busyBy must be a string or absent")
    _expect(not health["currentJob"] or health["busy"], "health.busy must be true while currentJob is set")
    resident = health.get("resident")
    _expect(resident is None or isinstance(resident, bool), "health.resident must be a boolean or absent")
    _expect(not resident or (health["busy"] and not health["currentJob"]),
            "health.resident means busy with no current job")
    _expect(isinstance(health.get("outputDir"), str), "health.outputDir must be a string")


def _recipes(run: _Run) -> None:
    recipes = run.client.recipes()
    _expect(isinstance(recipes, list) and recipes, "recipes must be a non-empty list")
    for recipe in recipes:
        name = recipe.get("name")
        _expect(isinstance(name, str) and name, "recipe.name must be a non-empty string")
        _expect(isinstance(recipe.get("label"), str), f"recipe {name} needs a label")
        _expect(isinstance(recipe.get("paramsSchema"), dict), f"recipe {name} needs a paramsSchema object")
        _expect(isinstance(recipe.get("needsReview"), bool), f"recipe {name} needs a needsReview boolean")
    _expect(any(recipe["name"] == "selftest" for recipe in recipes), "the selftest recipe is missing")


def _rejects_bad_input(run: _Run) -> None:
    _expect_status(lambda: run.client.submit("no-such-recipe"), 400, "an unknown recipe")
    _expect_status(lambda: run.client.request("POST", "/api/jobs", json={"recipe": "selftest", "params": []}),
                   400, "params that are not an object")


def _unknown_job(run: _Run) -> None:
    _expect_status(lambda: run.client.job("no-such-job"), 404, "an unknown job id")


def _rejects_foreign_origin(run: _Run) -> None:
    _expect_status(lambda: run.client.request("GET", "/api/health", headers={"Origin": FOREIGN_ORIGIN}),
                   403, "a request from another origin")


def _selftest_runs(run: _Run) -> None:
    created = run.client.submit("selftest", {"steps": 2, "stepSeconds": 1.0})
    _check_record(created)
    _expect(created["state"] in ("queued", "running"), f"a new job is {created['state']!r}")
    job_id = created["id"]

    seen_as_current = seen_busy = False

    def watch(record: dict[str, Any]) -> None:
        nonlocal seen_as_current, seen_busy
        if record.get("state") == "running":
            health = run.client.health()
            seen_as_current = seen_as_current or health.get("currentJob") == job_id
            seen_busy = seen_busy or health.get("busy") is True

    final = run.wait(job_id, timeout=30.0, on_poll=watch)
    _check_done_record(final)
    _expect(seen_as_current, "health.currentJob never named the job while it was running")
    _expect(seen_busy, "health.busy was never true while the job was running")
    after = run.client.health()
    _expect(after.get("currentJob") != job_id, "health.currentJob still names a finished job")
    # 상주 모델 서버를 쥔 엔진은 끝난 뒤에도 busy다. 그때는 resident로 그렇다고 말해야 한다.
    _expect(after.get("busy") is False or after.get("resident") is True,
            "health.busy is still true after the job finished, and the engine does not say it is resident")
    run.done_job = final


def _cancel_stops_a_running_job(run: _Run) -> None:
    # 10초면 스스로 끝나는 작업이다 — 취소를 무시하는 엔진에서도 대기열을 막은 채
    # 남지 않고, 그때는 `done`으로 끝나 아래 판정에서 드러난다.
    job_id = run.client.submit("selftest", {"steps": 2, "stepSeconds": 5.0})["id"]
    running = run.wait(job_id, until=frozenset({"running"}), timeout=15.0)
    _expect(running["state"] == "running", f"the job never ran: {running['state']!r}")
    answered = run.client.cancel(job_id)
    _expect(answered.get("state") in ("cancelling", "cancelled"),
            f"cancel answered with state {answered.get('state')!r}")
    final = run.wait(job_id, timeout=20.0)
    _check_record(final)
    _expect(final["state"] == "cancelled", f"a cancelled job ended {final['state']!r}")
    _expect(_is_timestamp(final["finishedAt"]), "a cancelled job needs finishedAt")


def _failure_is_classified(run: _Run) -> None:
    job_id = run.client.submit("selftest", {"steps": 2, "stepSeconds": 0.2, "failAt": 1})["id"]
    final = run.wait(job_id, timeout=30.0)
    _check_record(final)
    _expect(final["state"] == "failed", f"failAt ended {final['state']!r}")
    _expect(final["errorCode"] == "engine_error", f"errorCode is {final['errorCode']!r}, expected 'engine_error'")
    _expect(final["errorCode"] in ERROR_CODES and isinstance(final["error"], str) and final["error"],
            "a failed job needs an error message")


def _review_is_recorded(run: _Run) -> None:
    _expect(run.done_job is not None, "no finished selftest job to review")
    job_id, asset_id = run.done_job["id"], run.done_job["assets"][0]["id"]

    def reviewed(status: str) -> str:
        record = run.client.review(job_id, asset_id, status)
        return next(asset for asset in record["assets"] if asset["id"] == asset_id)["review"]

    for status in ("rejected", "pending"):
        _expect(status in REVIEW_STATES and reviewed(status) == status, f"review {status!r} was not recorded")
    _expect_status(lambda: run.client.review(job_id, asset_id, "looks-fine"), 400, "an unknown review status")
    _expect_status(lambda: run.client.review(job_id, "no-such-asset", "pending"), 404, "an unknown asset id")


def _finding_review_is_recorded(run: _Run) -> None:
    _expect(run.done_job is not None, "no finished selftest job to review")
    job_id, finding_id = run.done_job["id"], run.done_job["findings"][0]["id"]

    def reviewed(status: str) -> str:
        record = run.client.review_finding(job_id, finding_id, status)
        return next(finding for finding in record["findings"] if finding["id"] == finding_id)["review"]

    for status in ("cleared", "pending"):
        _expect(status in FINDING_REVIEW_STATES and reviewed(status) == status, f"finding review {status!r} was not recorded")
    _expect_status(lambda: run.client.review_finding(job_id, finding_id, "approved"), 400, "an unknown finding review status")
    _expect_status(lambda: run.client.review_finding(job_id, "no-such-finding", "pending"), 404, "an unknown finding id")


def _lists_recent_jobs(run: _Run) -> None:
    jobs = run.client.jobs(limit=2)
    _expect(isinstance(jobs, list) and 1 <= len(jobs) <= 2, f"limit=2 returned {len(jobs)} jobs")
    for job in jobs:
        _check_record(job)


CHECKS: tuple[tuple[str, Callable[[_Run], None]], ...] = (
    ("health", _health),
    ("recipes", _recipes),
    ("rejects_bad_input", _rejects_bad_input),
    ("unknown_job", _unknown_job),
    ("rejects_foreign_origin", _rejects_foreign_origin),
    ("selftest_runs", _selftest_runs),
    ("cancel_stops_a_running_job", _cancel_stops_a_running_job),
    ("failure_is_classified", _failure_is_classified),
    ("review_is_recorded", _review_is_recorded),
    ("finding_review_is_recorded", _finding_review_is_recorded),
    ("lists_recent_jobs", _lists_recent_jobs),
)


def run_checks(
    client: EngineClient,
    *,
    poll: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[CheckResult]:
    run = _Run(client=client, poll=poll, sleep=sleep, clock=clock)
    results: list[CheckResult] = []
    for name, check in CHECKS:
        try:
            check(run)
        except _Failed as failure:
            results.append(CheckResult(name, False, str(failure)))
        except Exception as error:  # noqa: BLE001 — a malformed answer is a failed check, not a crash
            results.append(CheckResult(name, False, f"{type(error).__name__}: {error}"))
        else:
            results.append(CheckResult(name, True))
    return results


def _working(client: EngineClient) -> str | None:
    """What the engine is working on, or None when it is idle, only holds a resident
    model (selftest does not need it), or does not answer (the checks then report it)."""
    try:
        health = client.health()
    except Exception:  # noqa: BLE001 — a broken health answer is the health check's to report
        return None
    if not isinstance(health, dict) or not is_busy(health):
        return None
    if not health.get("currentJob") and health.get("resident") is True:
        return None
    return f"currentJob={health.get('currentJob')} busyBy={health.get('busyBy')}"


def main(argv: list[str] | None = None, *, client: EngineClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ark-engine-check", description=__doc__.splitlines()[0])
    parser.add_argument("base_url", help="engine address, e.g. http://127.0.0.1:47832")
    args = parser.parse_args(argv)

    client = client or EngineClient(args.base_url)
    try:
        # selftest는 같은 대기열에 서므로, 일하는 엔진에서는 실제 작업 뒤에서 시간 초과로 실패한다.
        working = _working(client)
        if working is not None:
            print(f"BUSY  the engine is working ({working}); no check ran — run again when it is idle")
            return EXIT_BUSY
        results = run_checks(client)
    finally:
        client.close()
    for result in results:
        print(f"{'PASS' if result.ok else 'FAIL'}  {result.name}" + (f"  — {result.detail}" if result.detail else ""))
    failed = sum(not result.ok for result in results)
    print(f"\n{len(results) - failed}/{len(results)} passed (contract v{CONTRACT_VERSION})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
