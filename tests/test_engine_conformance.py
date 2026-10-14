"""The contract check passes a conforming engine and names what a flawed one misses.

The engine here is an in-memory fake behind `httpx.MockTransport`. Its clock only
moves when the checker sleeps, so the suite runs instantly and offline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from arkagent.engines.client import EngineClient, EngineTimeout
from arkagent.engines.conformance import CHECKS, main, run_checks

BASE_URL = "http://127.0.0.1:47999"


class FakeEngine:
    def __init__(self, *flaws: str) -> None:
        self.flaws = set(flaws)
        self.now = 0.0
        self.jobs: dict[str, dict] = {}

    # ── time ────────────────────────────────────────────────────────────────
    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def client(self) -> EngineClient:
        http = httpx.Client(transport=httpx.MockTransport(self._handle), base_url=BASE_URL)
        return EngineClient(BASE_URL, http=http)

    # ── queue ───────────────────────────────────────────────────────────────
    def _stamp(self) -> str:
        return f"2026-10-14T10:00:{int(self.now) % 60:02d}+09:00"

    def _running(self) -> dict | None:
        return next((job for job in self.jobs.values() if job["state"] in ("running", "cancelling")), None)

    def _tick(self) -> None:
        current = self._running()
        if current is not None:
            self._advance(current)
        if self._running() is None:
            queued = next((job for job in self.jobs.values() if job["state"] == "queued"), None)
            if queued is not None:
                queued.update(state="running", startedAt=self._stamp(), _started=self.now)

    def _advance(self, job: dict) -> None:
        elapsed = self.now - job["_started"]
        if job["state"] == "cancelling":
            job.update(state="cancelled", finishedAt=self._stamp())
        elif elapsed <= 0:
            return
        elif job["_failAt"] and elapsed >= (job["_failAt"] - 1) * job["_stepSeconds"]:
            code = "boom" if "wrong_error_code" in self.flaws else "engine_error"
            job.update(state="failed", finishedAt=self._stamp(), error="selftest was told to fail", errorCode=code)
        elif elapsed >= job["_steps"] * job["_stepSeconds"]:
            self._finish(job)

    def _finish(self, job: dict) -> None:
        stage_count = 1 if "one_stage" in self.flaws else job["_steps"]
        job.update(
            state="done",
            finishedAt=self._stamp(),
            stages=[
                {"name": f"step-{index + 1}", "label": f"단계 {index + 1}", "state": "done", "progress": 1.0,
                 "seconds": job["_stepSeconds"], "peakMemoryBytes": None}
                for index in range(stage_count)
            ],
            assets=[{
                "id": "a01", "kind": "text", "role": "final",
                "file": "/abs/selftest.txt" if "absolute_paths" in self.flaws else "final/selftest.txt",
                "review": "approved" if "approves_itself" in self.flaws else "pending",
            }],
            findings=[] if "no_findings" in self.flaws else [{
                "id": "f01", "severity": "warn", "code": "selftest.sample", "params": {"step": 1},
                "where": "1단계", "assetId": "a01", "evidence": ["final/selftest.txt"],
            }],
        )

    @staticmethod
    def _public(job: dict) -> dict:
        return {key: value for key, value in job.items() if not key.startswith("_")}

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _handle(self, request: httpx.Request) -> httpx.Response:
        if "origin" in request.headers and "no_origin_check" not in self.flaws:
            return httpx.Response(403, json={"detail": "다른 출처의 요청은 받지 않습니다."})
        self._tick()
        parts = request.url.path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}

        if parts == ["api", "health"]:
            running = self._running()
            current = running["id"] if running else None
            if "stale_current_job" in self.flaws and self.jobs:
                current = next(iter(self.jobs))
            health = {"ok": True, "engine": "fake", "version": "0.0.0", "contract": 0,
                      "currentJob": current, "outputDir": "/tmp/fake-engine"}
            if "no_contract" in self.flaws:
                del health["contract"]
            return httpx.Response(200, json=health)

        if parts == ["api", "recipes"]:
            name = "something-else" if "no_selftest" in self.flaws else "selftest"
            return httpx.Response(200, json={"recipes": [
                {"name": name, "label": "계약 검사", "paramsSchema": {"type": "object"}, "needsReview": False},
            ]})

        if parts == ["api", "jobs"] and request.method == "GET":
            limit = int(request.url.params.get("limit", "100"))
            newest_first = list(self.jobs.values())[::-1][:limit]
            return httpx.Response(200, json={"jobs": [self._public(job) for job in newest_first]})

        if parts == ["api", "jobs"] and request.method == "POST":
            params = body.get("params", {})
            known = body.get("recipe") == "selftest" and "no_selftest" not in self.flaws
            if not isinstance(params, dict) or not (known or "accepts_bad_recipe" in self.flaws):
                return httpx.Response(400, json={"detail": "받을 수 없는 요청입니다."})
            job_id = f"job-{len(self.jobs) + 1}"
            self.jobs[job_id] = {
                "id": job_id, "recipe": body["recipe"], "title": "계약 검사", "params": params,
                "dir": f"jobs/{job_id}" if "relative_dir" in self.flaws else f"/tmp/fake-engine/jobs/{job_id}",
                "state": "queued", "createdAt": self._stamp(), "startedAt": None, "finishedAt": None,
                "stages": [], "assets": [], "findings": [], "error": None, "errorCode": None, "logTail": [],
                "_steps": int(params.get("steps", 2)), "_stepSeconds": float(params.get("stepSeconds", 0.5)),
                "_failAt": params.get("failAt"), "_started": None,
            }
            self._tick()
            return httpx.Response(201, json=self._public(self.jobs[job_id]))

        job = self.jobs.get(parts[2]) if len(parts) >= 3 and parts[:2] == ["api", "jobs"] else None
        if job is None:
            return httpx.Response(404, json={"detail": "작업을 찾을 수 없습니다."})

        if len(parts) == 3:
            return httpx.Response(200, json=self._public(job))

        if parts[3:] == ["cancel"]:
            if "ignores_cancel" not in self.flaws:
                if job["state"] == "queued":
                    job.update(state="cancelled", finishedAt=self._stamp())
                elif job["state"] == "running":
                    job["state"] = "cancelling"
            return httpx.Response(200, json=self._public(job))

        if len(parts) == 6 and parts[3] == "assets" and parts[5] == "review" and "review_unsupported" not in self.flaws:
            if body.get("status") not in ("pending", "approved", "rejected"):
                return httpx.Response(400, json={"detail": "status가 올바르지 않습니다."})
            asset = next((item for item in job["assets"] if item["id"] == parts[4]), None)
            if asset is None:
                return httpx.Response(404, json={"detail": "에셋을 찾을 수 없습니다."})
            asset["review"] = body["status"]
            return httpx.Response(200, json=self._public(job))

        return httpx.Response(404, json={"detail": "없는 경로입니다."})


def _failed(engine: FakeEngine) -> set[str]:
    results = run_checks(engine.client(), poll=0.2, sleep=engine.sleep, clock=engine.clock)
    assert [result.name for result in results] == [name for name, _ in CHECKS]
    return {result.name for result in results if not result.ok}


def test_conforming_engine_passes_every_check():
    engine = FakeEngine()

    results = run_checks(engine.client(), poll=0.2, sleep=engine.sleep, clock=engine.clock)

    assert [(result.name, result.detail) for result in results if not result.ok] == []
    assert len(results) == len(CHECKS)


# 완료된 selftest 작업이 없으면 승인 기록 검사도 볼 대상이 없어 함께 실패한다.
_SELFTEST = {"selftest_runs", "review_is_recorded"}


@pytest.mark.parametrize(
    ("flaw", "expected"),
    [
        ("no_contract", {"health"}),
        ("accepts_bad_recipe", {"rejects_bad_input"}),
        ("no_origin_check", {"rejects_foreign_origin"}),
        ("one_stage", _SELFTEST),
        ("absolute_paths", _SELFTEST),
        ("approves_itself", _SELFTEST),
        ("no_findings", _SELFTEST),
        ("stale_current_job", _SELFTEST),
        ("ignores_cancel", {"cancel_stops_a_running_job"}),
        ("wrong_error_code", {"failure_is_classified"}),
        ("review_unsupported", {"review_is_recorded"}),
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
                      "review_is_recorded", "lists_recent_jobs"}


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
