"""An in-memory engine that speaks docs/ENGINE-CONTRACT.md, behind `httpx.MockTransport`.

Its clock only moves when the caller sleeps, so tests run instantly and offline.
Several engines can share one `FakeClock` to stand for one machine. `flaws` name
the ways an engine can break the contract.
"""

from __future__ import annotations

import json

import httpx

from arkagent.engines.client import EngineClient

BASE_URL = "http://127.0.0.1:47999"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now


class FakeEngine:
    def __init__(self, *flaws: str, name: str = "fake", time: FakeClock | None = None) -> None:
        self.flaws = set(flaws)
        self.name = name
        self.time = time or FakeClock()
        self.jobs: dict[str, dict] = {}
        self.outside: str | None = None  # 대기열 밖의 일(앱이 직접 돌리는 작업)의 busyBy
        self.resident: str | None = None  # 일 없이 모델을 쥔(계약 위반) 모델 서버의 busyBy
        self.recipes = [{
            "name": "something-else" if "no_selftest" in self.flaws else "selftest",
            "label": "계약 검사", "paramsSchema": {"type": "object"}, "needsReview": False,
        }]

    # ── time ────────────────────────────────────────────────────────────────
    @property
    def now(self) -> float:
        return self.time.now

    def sleep(self, seconds: float) -> None:
        self.time.sleep(seconds)

    def clock(self) -> float:
        return self.time.clock()

    def client(self, base_url: str = BASE_URL) -> EngineClient:
        http = httpx.Client(transport=httpx.MockTransport(self._handle), base_url=base_url)
        return EngineClient(base_url, http=http)

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
                 "seconds": job["_stepSeconds"], "peakMemoryBytes": None,
                 **({"peakMemoryMethod": "guess"} if "unknown_memory_method" in self.flaws else {})}
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
                "review": "cleared" if "clears_itself" in self.flaws else "pending",
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
            working = running is not None or self.outside is not None
            health = {"ok": True, "engine": self.name, "version": "0.0.0", "contract": 0,
                      "busy": working or self.resident is not None,
                      "currentJob": current, "outputDir": f"/tmp/{self.name}-engine"}
            if self.outside is not None or self.resident is not None:
                health["busyBy"] = self.outside or self.resident
            if self.resident is not None and not working and "hides_resident" not in self.flaws:
                health["resident"] = True
            for flaw, key in (("no_contract", "contract"), ("no_busy", "busy")):
                if flaw in self.flaws:
                    del health[key]
            return httpx.Response(200, json=health)

        if parts == ["api", "recipes"]:
            return httpx.Response(200, json={"recipes": self.recipes})

        if parts == ["api", "jobs"] and request.method == "GET":
            limit = int(request.url.params.get("limit", "100"))
            newest_first = list(self.jobs.values())[::-1][:limit]
            return httpx.Response(200, json={"jobs": [self._public(job) for job in newest_first]})

        if parts == ["api", "jobs"] and request.method == "POST":
            params = body.get("params", {})
            known = body.get("recipe") == "selftest" and "no_selftest" not in self.flaws
            if not isinstance(params, dict) or not (known or "accepts_bad_recipe" in self.flaws):
                return httpx.Response(400, json={"detail": "받을 수 없는 요청입니다."})
            if "refuses_jobs" in self.flaws:
                return httpx.Response(409, json={"detail": "지금은 받을 수 없습니다."})
            job_id = f"job-{len(self.jobs) + 1}"
            self.jobs[job_id] = {
                "id": job_id, "recipe": body["recipe"], "title": "계약 검사", "params": params,
                "dir": f"jobs/{job_id}" if "relative_dir" in self.flaws else f"/tmp/{self.name}-engine/jobs/{job_id}",
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

        if len(parts) == 6 and parts[3] == "findings" and parts[5] == "review" \
                and "finding_review_unsupported" not in self.flaws:
            if body.get("status") not in ("pending", "cleared"):
                return httpx.Response(400, json={"detail": "status가 올바르지 않습니다."})
            finding = next((item for item in job["findings"] if item["id"] == parts[4]), None)
            if finding is None:
                return httpx.Response(404, json={"detail": "확인 항목을 찾을 수 없습니다."})
            finding["review"] = body["status"]
            return httpx.Response(200, json=self._public(job))

        return httpx.Response(404, json={"detail": "없는 경로입니다."})
