"""The gateway core — every registered engine behind one small surface.

The CLI (and later the MCP surface) call this and only format what comes back.
It holds the one rule an engine cannot see for itself: on one machine, one
engine works at a time.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from ..core.errors import ArkAgentError, ConflictError, ValidationError
from ..engines.client import EngineClient, EngineError, EngineTimeout, EngineUnreachable, is_busy


@dataclass(frozen=True)
class EngineStatus:
    name: str
    state: str  # "off" | "idle" | "busy" | "error"
    contract: object = None  # health.contract 그대로
    job_id: str | None = None  # 대기열에서 실행 중인 작업. busy인데 없으면 대기열 밖의 일이다
    job: dict[str, Any] | None = None  # 실행 중인 작업 기록. 읽지 못했으면 None
    busy_by: str | None = None
    resident: bool = False  # 일이 아니라 쉬는 동안 올라 있는 상주 모델 때문에 busy다
    detail: str = ""


class UnknownEngine(ValidationError):
    code = "engine.unknown"


class UnknownRecipe(ValidationError):
    code = "recipe.unknown"


class BadJobRef(ValidationError):
    code = "job.bad_ref"


class Blocked(ConflictError):
    """Another engine is working, so the job was not submitted."""

    code = "engine.blocked"

    def __init__(self, blockers: Sequence[EngineStatus]) -> None:
        super().__init__("another engine is working")
        self.blockers = list(blockers)


class WaitTimeout(ArkAgentError):
    """The wait ran out. Nothing was undone: a submitted job keeps running."""

    code = "wait.timeout"
    http = 504

    def __init__(self, seconds: float, *, ref: str | None = None, record: dict[str, Any] | None = None,
                 blockers: Sequence[EngineStatus] = ()) -> None:
        super().__init__(f"still waiting after {seconds:g}s", params={"seconds": seconds})
        self.ref = ref
        self.record = record
        self.blockers = list(blockers)


def job_ref(engine: str, job_id: str) -> str:
    return f"{engine}:{job_id}"


def parse_ref(ref: str) -> tuple[str, str]:
    engine, separator, job_id = ref.partition(":")
    if not (separator and engine and job_id):
        raise BadJobRef(f"not an <engine>:<job id> reference: {ref!r}", params={"ref": ref})
    return engine, job_id


class Gateway:
    def __init__(
        self,
        engines: Mapping[str, str],
        *,
        connect: Callable[[str], EngineClient] = EngineClient,
        poll: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.addresses = dict(engines)
        self.poll = poll
        self.sleep = sleep
        self.clock = clock
        self._connect = connect
        self._clients: dict[str, EngineClient] = {}

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    @contextmanager
    def _engine(self, name: str) -> Iterator[EngineClient]:
        """The engine's client. An error raised while using it names the engine."""
        if name not in self.addresses:
            raise UnknownEngine(f"no engine named {name!r}", params={"engine": name, "known": sorted(self.addresses)})
        if name not in self._clients:
            self._clients[name] = self._connect(self.addresses[name])
        try:
            yield self._clients[name]
        except ArkAgentError as error:
            error.params.setdefault("engine", name)
            raise

    # ── engines ─────────────────────────────────────────────────────────────
    def engine(self, name: str) -> EngineStatus:
        try:
            with self._engine(name) as client:
                health = client.health()
                if not is_busy(health):
                    return EngineStatus(name, "idle", contract=health.get("contract"))
                current = health.get("currentJob")
                busy_by = health.get("busyBy")
                return EngineStatus(name, "busy", contract=health.get("contract"),
                                    job_id=str(current) if current else None,
                                    job=self._peek(client, str(current)) if current else None,
                                    busy_by=busy_by if isinstance(busy_by, str) else None,
                                    resident=health.get("resident") is True)
        except EngineUnreachable:
            return EngineStatus(name, "off")
        except EngineError as error:
            return EngineStatus(name, "error", detail=str(error))
        except (httpx.HTTPError, ValueError, AttributeError) as error:  # 느린 응답, JSON이 아닌 응답
            return EngineStatus(name, "error", detail=type(error).__name__)

    def engines(self) -> list[EngineStatus]:
        return [self.engine(name) for name in self.addresses]

    @staticmethod
    def _peek(client: EngineClient, job_id: str) -> dict[str, Any] | None:
        try:
            return client.job(job_id)
        except (ArkAgentError, httpx.HTTPError, ValueError):
            return None

    def _blockers(self, target: str) -> list[EngineStatus]:
        """The other engines that are working, or whose state cannot be read.
        Only an engine known to be off or idle is taken as holding no model memory."""
        statuses = self.engines()
        if next(status for status in statuses if status.name == target).state == "off":
            raise EngineUnreachable(f"engine {target} is off", params={"engine": target})
        return [status for status in statuses if status.name != target and status.state in ("busy", "error")]

    # ── recipes ─────────────────────────────────────────────────────────────
    def recipes(self, engine: str) -> list[dict[str, Any]]:
        with self._engine(engine) as client:
            return client.recipes()

    def recipe(self, engine: str, name: str) -> dict[str, Any]:
        recipes = self.recipes(engine)
        found = next((recipe for recipe in recipes if recipe.get("name") == name), None)
        if found is None:
            raise UnknownRecipe(f"engine {engine} has no recipe {name!r}", params={
                "engine": engine, "recipe": name, "known": [recipe.get("name") for recipe in recipes]})
        return found

    # ── jobs ────────────────────────────────────────────────────────────────
    def submit(
        self,
        engine: str,
        recipe: str,
        params: dict[str, Any] | None = None,
        *,
        when_idle: bool = False,
        timeout: float | None = None,
        on_wait: Callable[[list[EngineStatus]], None] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Queue a job and return its reference and record.

        While another engine works the job is not submitted: `Blocked`, or with
        `when_idle` a wait until the machine is free. The target engine's own
        running job does not block — its queue keeps the order.
        """
        with self._engine(engine):
            pass  # 모르는 엔진은 다른 엔진을 살피기 전에 걸러 낸다
        deadline = self.clock() + (math.inf if timeout is None else timeout)
        announced = False
        while blockers := self._blockers(engine):
            if not when_idle:
                raise Blocked(blockers)
            if self.clock() >= deadline:
                raise WaitTimeout(timeout or 0.0, blockers=blockers)
            if on_wait is not None and not announced:
                on_wait(blockers)
                announced = True
            self.sleep(self.poll)
        with self._engine(engine) as client:
            record = client.submit(recipe, params)
        return job_ref(engine, record["id"]), record

    def job(self, ref: str) -> dict[str, Any]:
        engine, job_id = parse_ref(ref)
        with self._engine(engine) as client:
            return client.job(job_id)

    def wait(self, ref: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Re-read the job until it ends. On `WaitTimeout` the job keeps running."""
        engine, job_id = parse_ref(ref)
        last: dict[str, Any] | None = None

        def remember(record: dict[str, Any]) -> None:
            nonlocal last
            last = record

        try:
            with self._engine(engine) as client:
                return client.wait(job_id, timeout=math.inf if timeout is None else timeout, poll=self.poll,
                                   sleep=self.sleep, clock=self.clock, on_poll=remember)
        except EngineTimeout as error:
            raise WaitTimeout(timeout or 0.0, ref=ref, record=last) from error

    def cancel(self, ref: str) -> dict[str, Any]:
        engine, job_id = parse_ref(ref)
        with self._engine(engine) as client:
            return client.cancel(job_id)
