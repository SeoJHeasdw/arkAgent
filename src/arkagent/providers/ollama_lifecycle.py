"""Task-scoped Ollama model residency.

Ollama's request-level ``keep_alive`` is intentionally kept finite as a crash
backstop.  During a healthy runtime this manager adds the missing application
lifecycle around it:

* nested leases keep a model resident across a whole ReAct turn (including tool
  execution and approval waits), not merely across one HTTP request;
* if such a wait approaches the finite server ``keep_alive``, an empty preload
  heartbeat refreshes it (normal short turns send no heartbeat);
* the final lease starts a short grace period so the post-turn title/review calls
  can reuse the already-loaded model;
* once that grace expires an empty ``/api/generate`` request with
  ``keep_alive: 0`` explicitly frees the model;
* model switches retire the old key, and process shutdown unloads every model the
  runtime actually touched.

The manager is owned by ``ProviderRegistry`` and shared by the main and utility
Ollama provider instances.  It deliberately does not own inference traffic; its
small private HTTP client is used only for lifecycle unload/heartbeat commands.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

log = logging.getLogger("arkagent.providers.ollama.lifecycle")


@dataclass(frozen=True)
class OllamaModelKey:
    base_url: str
    model: str

    @classmethod
    def create(cls, base_url: str | None, model: str) -> "OllamaModelKey":
        return cls((base_url or "http://localhost:11434").rstrip("/"), model)


@dataclass
class _ModelState:
    leases: int = 0
    touched: bool = False
    retired: bool = False
    idle_seconds: float = 60.0
    request_keep_alive: str | int | float = "5m"
    generation: int = 0
    unload_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.idle.set()


UnloadFn = Callable[[OllamaModelKey], Awaitable[None]]
RefreshFn = Callable[[OllamaModelKey, str | int | float], Awaitable[None]]

_DURATION_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_DURATION_MULTIPLIER = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _keep_alive_seconds(value: str | int | float) -> float | None:
    """Parse Ollama duration forms enough to schedule a conservative heartbeat."""

    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        match = _DURATION_RE.match(str(value))
        if match is None:
            return None
        number, unit = match.groups()
        seconds = float(number) * _DURATION_MULTIPLIER.get((unit or "s").lower(), 1.0)
    # Ollama defines negative as indefinite and zero as immediate unload; neither
    # needs (or benefits from) a refresh timer.
    return seconds if seconds > 0 else None


class OllamaLifecycleManager:
    """Reference-counted, race-safe residency manager for Ollama model keys."""

    def __init__(
        self,
        *,
        unload: UnloadFn | None = None,
        refresh: RefreshFn | None = None,
    ) -> None:
        self._states: dict[OllamaModelKey, _ModelState] = {}
        self._unload_override = unload
        self._refresh_override = refresh
        self._client = None
        self._closing = False
        self._shutdown_complete = False
        self._shutdown_lock = asyncio.Lock()

    @asynccontextmanager
    async def lease(
        self,
        key: OllamaModelKey,
        *,
        idle_seconds: float,
        touches_model: bool = False,
        request_keep_alive: str | int | float = "5m",
    ) -> AsyncIterator[None]:
        """Hold ``key`` in memory for one logical scope.

        ``touches_model`` is true only around an actual inference HTTP request.
        An outer turn lease therefore does not accidentally load an unused model
        merely so the delayed cleanup can unload it again.
        """

        state = self._states.setdefault(key, _ModelState())
        async with state.lock:
            # If the delayed task is still sleeping, invalidate and cancel it
            # before admitting the new request.  If it is already issuing the
            # unload it owns this lock; we wait for that bounded call to finish,
            # then the inference request can safely cold-load again.
            state.generation += 1
            pending = state.unload_task
            state.unload_task = None
            if pending is not None and not pending.done():
                pending.cancel()
                # 대기 중이던 unload 취소 = 턴 종료 후 후속 작업(제목/학습 리뷰,
                # 또는 유예 내 새 턴)이 로드된 모델을 재사용해 상주가 연장되는
                # 순간이다 — residency 진단의 반대쪽 절반(성공 로그는 _delayed_unload).
                log.info("Ollama unload grace for %s cancelled — follow-up work reuses the loaded model", key.model)
            state.leases += 1
            state.idle.clear()
            state.idle_seconds = max(0.0, float(idle_seconds))
            state.request_keep_alive = request_keep_alive
            if touches_model:
                # The inference request itself refreshes Ollama's timer. Replace
                # any pending idle heartbeat with one scheduled from its end.
                heartbeat = state.heartbeat_task
                state.heartbeat_task = None
                if heartbeat is not None and not heartbeat.done():
                    heartbeat.cancel()
                state.touched = True
            elif state.touched and state.heartbeat_task is None:
                # A follow-up logical turn can cancel the post-turn unload before
                # its first model request. Keep the prior residency alive even if
                # prefetch/tool assembly itself becomes unusually long.
                self._schedule_heartbeat_locked(key, state)

        try:
            yield
        finally:
            async with state.lock:
                state.leases = max(0, state.leases - 1)
                if state.leases == 0:
                    state.idle.set()
                    heartbeat = state.heartbeat_task
                    state.heartbeat_task = None
                    if heartbeat is not None and not heartbeat.done():
                        heartbeat.cancel()
                    if state.touched:
                        delay = 0.0 if state.retired or self._closing else state.idle_seconds
                        self._schedule_unload_locked(key, state, delay)
                elif state.touched and state.heartbeat_task is None:
                    # A real request ended but an outer ReAct lease remains. The
                    # heartbeat runs only if that idle lasts near keep_alive.
                    self._schedule_heartbeat_locked(key, state)

    async def activate(self, key: OllamaModelKey) -> None:
        """Make a previously retired key current again (model switch A→B→A)."""

        state = self._states.get(key)
        if state is None:
            return
        async with state.lock:
            state.retired = False
            state.generation += 1
            pending = state.unload_task
            state.unload_task = None
            if pending is not None and not pending.done():
                pending.cancel()

    async def retire(self, key: OllamaModelKey) -> None:
        """Unload an old configured model now, or as soon as its active turn ends."""

        state = self._states.get(key)
        if state is None:
            return
        task: asyncio.Task[None] | None = None
        async with state.lock:
            state.retired = True
            if state.leases == 0 and state.touched:
                task = self._schedule_unload_locked(key, state, 0.0)
        if task is not None:
            # Settings should return only after an idle old model has actually
            # received its unload command.  Failures remain best-effort inside
            # the task; the finite request keep_alive is the final safety net.
            await task

    def cancel_pending(self) -> None:
        """Synchronous legacy-shutdown hook: stop timers without starting I/O."""

        for state in self._states.values():
            state.generation += 1
            if state.unload_task is not None and not state.unload_task.done():
                state.unload_task.cancel()
            state.unload_task = None
            if state.heartbeat_task is not None and not state.heartbeat_task.done():
                state.heartbeat_task.cancel()
            state.heartbeat_task = None

    async def shutdown(self, *, drain_seconds: float = 2.0) -> None:
        """Drain short active scopes, then explicitly unload every touched key."""

        # `/shutdown` and ASGI lifespan teardown can legitimately race.  Serialize
        # them so no model receives duplicate stop commands and the private client
        # cannot be closed underneath another shutdown call.
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._closing = True
            self.cancel_pending()

            waits = [state.idle.wait() for state in self._states.values() if state.leases]
            if waits:
                try:
                    await asyncio.wait_for(asyncio.gather(*waits), timeout=max(0.0, drain_seconds))
                except asyncio.TimeoutError:
                    log.warning("forcing Ollama unload with active leases during shutdown")

            # Releases that raced with the wait may have scheduled immediate tasks.
            # Cancel those and perform one deterministic unload per touched key.
            self.cancel_pending()
            for key, state in self._states.items():
                async with state.lock:
                    if not state.touched:
                        continue
                    try:
                        await self._unload(key)
                    except Exception:  # noqa: BLE001 - teardown must remain best-effort
                        log.warning("Ollama shutdown unload failed for %s", key.model, exc_info=True)
                    else:
                        state.touched = False

            if self._client is not None:
                await self._client.aclose()
                self._client = None
            self._shutdown_complete = True

    def _schedule_unload_locked(
        self, key: OllamaModelKey, state: _ModelState, delay: float
    ) -> asyncio.Task[None]:
        state.generation += 1
        generation = state.generation
        pending = state.unload_task
        if pending is not None and not pending.done():
            pending.cancel()
        task = asyncio.create_task(self._delayed_unload(key, state, generation, delay))
        state.unload_task = task
        return task

    def _schedule_heartbeat_locked(self, key: OllamaModelKey, state: _ModelState) -> None:
        keep_alive_seconds = _keep_alive_seconds(state.request_keep_alive)
        if keep_alive_seconds is None:
            return
        # Refresh with 20% headroom. One-second floor prevents a pathological
        # tight loop when a manually configured duration is extremely small.
        interval = max(1.0, keep_alive_seconds * 0.8)
        state.heartbeat_task = asyncio.create_task(self._heartbeat(key, state, interval))

    async def _heartbeat(self, key: OllamaModelKey, state: _ModelState, interval: float) -> None:
        delay = interval
        try:
            while True:
                await asyncio.sleep(delay)
                async with state.lock:
                    if state.leases == 0 or not state.touched:
                        return
                    try:
                        await self._refresh(key, state.request_keep_alive)
                    except Exception:  # noqa: BLE001 - inference remains authoritative
                        log.warning("Ollama active-lease heartbeat failed for %s", key.model, exc_info=True)
                        # A transient failure should retry before the original
                        # keep_alive window is exhausted, without a hot loop.
                        delay = min(10.0, max(1.0, interval * 0.2))
                    else:
                        delay = interval
        except asyncio.CancelledError:
            return
        finally:
            if state.heartbeat_task is asyncio.current_task():
                state.heartbeat_task = None

    async def _delayed_unload(
        self, key: OllamaModelKey, state: _ModelState, generation: int, delay: float
    ) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            async with state.lock:
                if state.generation != generation or state.leases or not state.touched:
                    return
                try:
                    await self._unload(key)
                except Exception:  # noqa: BLE001 - keep_alive remains the fallback
                    log.warning(
                        "Ollama idle unload failed for %s; finite keep_alive remains active",
                        key.model,
                        exc_info=True,
                    )
                else:
                    state.touched = False
                    # INFO인 이유: "턴이 끝나도 모델이 5분 상주한다"는 리포트가
                    # 왔을 때(2026-07-14) 명시 해제의 성공/실패가 콘솔에서 보이지
                    # 않아 진단이 불가능했다 — unload는 턴당 최대 1회라 시끄럽지
                    # 않고, 이 한 줄이 residency 문제를 자가 진단 가능하게 만든다.
                    log.info("Ollama model %s unloaded after %.0fs idle grace", key.model, delay)
        except asyncio.CancelledError:
            return
        finally:
            # Do not erase a newer timer installed after this generation.
            if state.generation == generation and state.unload_task is asyncio.current_task():
                state.unload_task = None

    async def _unload(self, key: OllamaModelKey) -> None:
        if self._unload_override is not None:
            await self._unload_override(key)
            return
        client = self._http_client()
        # Ollama's documented API unload contract: an empty generation request
        # with keep_alive=0.  stream:false makes this a single bounded response.
        response = await client.post(
            f"{key.base_url}/api/generate",
            json={"model": key.model, "keep_alive": 0, "stream": False},
        )
        response.raise_for_status()

    async def _refresh(self, key: OllamaModelKey, keep_alive: str | int | float) -> None:
        if self._refresh_override is not None:
            await self._refresh_override(key, keep_alive)
            return
        client = self._http_client()
        # Ollama documents an empty generation request as the preload/keep-loaded
        # operation. Since the model is already resident this only refreshes its
        # finite server timer; it does not generate tokens.
        response = await client.post(
            f"{key.base_url}/api/generate",
            json={"model": key.model, "keep_alive": keep_alive, "stream": False},
        )
        response.raise_for_status()

    def _http_client(self):
        if self._client is None:
            import httpx  # noqa: PLC0415 - optional provider dependency

            # 10초인 이유: unload/refresh는 state.lock을 쥔 채 실행되므로 이
            # 타임아웃이 곧 새 lease admission의 최악 대기 상한이다. 유휴 모델
            # unload는 실측 ~0.1초(Ollama 0.31.2)지만, 서버가 다른 모델을 로드
            # 중이면 명령이 큐에 걸린다 — 5초는 그 경우를 조용히 죽여 keep_alive
            # 만료(5분)까지 상주하게 만들었다.
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client
