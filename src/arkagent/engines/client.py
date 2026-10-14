"""HTTP client for a local engine speaking docs/ENGINE-CONTRACT.md.

One small synchronous client, used both by the contract check and by whatever
runs jobs on an engine. It knows the wire and nothing about any one engine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from ..core.errors import ArkAgentError

CONTRACT_VERSION = 0
JOB_STATES = frozenset({"queued", "running", "cancelling", "done", "failed", "cancelled"})
FINAL_STATES = frozenset({"done", "failed", "cancelled"})
REVIEW_STATES = frozenset({"pending", "approved", "rejected"})


class EngineError(ArkAgentError):
    """The engine answered with an error status."""

    code = "engine.http_error"
    http = 502

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"engine answered {status}: {detail}" if detail else f"engine answered {status}")
        self.status = status
        self.detail = detail


class EngineTimeout(ArkAgentError):
    code = "engine.timeout"
    http = 504


class EngineClient:
    def __init__(self, base_url: str, *, http: httpx.Client | None = None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, *, json: Any = None, headers: dict[str, str] | None = None) -> Any:
        response = self._http.request(method, path, json=json, headers=headers)
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", ""))
            except Exception:  # noqa: BLE001 — an error body need not be JSON
                detail = response.text[:200]
            raise EngineError(response.status_code, detail)
        return response.json()

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/api/health")

    def recipes(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/recipes")["recipes"]

    def submit(self, recipe: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("POST", "/api/jobs", json={"recipe": recipe, "params": params or {}})

    def job(self, job_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/jobs/{job_id}")

    def jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/jobs?limit={limit}")["jobs"]

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/jobs/{job_id}/cancel")

    def review(self, job_id: str, asset_id: str, status: str) -> dict[str, Any]:
        return self.request("POST", f"/api/jobs/{job_id}/assets/{asset_id}/review", json={"status": status})

    def wait(
        self,
        job_id: str,
        *,
        until: frozenset[str] = FINAL_STATES,
        timeout: float = 60.0,
        poll: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        on_poll: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Re-read the job until its state is in `until`. A final state always
        ends the wait, so waiting for "running" cannot hang on a job that failed."""
        deadline = clock() + timeout
        while True:
            record = self.job(job_id)
            if on_poll is not None:
                on_poll(record)
            if record.get("state") in until or record.get("state") in FINAL_STATES:
                return record
            if clock() >= deadline:
                raise EngineTimeout(f"job {job_id} is still {record.get('state')!r} after {timeout:g}s")
            sleep(poll)
