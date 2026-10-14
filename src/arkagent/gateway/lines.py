"""Short lines for a frontier model to read — the gateway's whole output format.

A line is stable words followed by `key=value` fields, never a sentence. Text an
engine wrote (an error detail, a location, a description) passes through quoted;
nothing here composes prose. Fields that have no value are left out.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..core.errors import ArkAgentError
from ..engines.client import CONTRACT_VERSION, FINAL_STATES
from .core import EngineStatus, job_ref

DEFAULT_TOP = 5  # `result`가 보여 주는 확인할 곳의 수
MAX_ASSETS = 20
MAX_ERROR_CHARS = 300


def _text(value: object) -> str:
    """A value as one token: bare when it can be, JSON when it holds spaces or structure."""
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value and not any(ch.isspace() or ch == '"' for ch in value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _fields(**pairs: object) -> list[str]:
    return [f"{key}={_text(value)}" for key, value in pairs.items() if value is not None]


def _seconds(start: object, end: object = None, *, now: datetime | None = None) -> float | None:
    try:
        began = datetime.fromisoformat(start)  # type: ignore[arg-type]
        ended = datetime.fromisoformat(end) if end else (now or datetime.now(began.tzinfo))  # type: ignore[arg-type]
        seconds = (ended - began).total_seconds()
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return f"{seconds:.0f}s" if seconds >= 10 else f"{seconds:.1f}s"


def _to_check(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings no person has cleared yet, `fail` first. An engine on the earlier
    contract has no `review` on findings, so all of its findings are still to check."""
    open_ = [finding for finding in findings if finding.get("review") != "cleared"]
    return sorted(open_, key=lambda finding: finding.get("severity") != "fail")


# ── engines ─────────────────────────────────────────────────────────────────
def engine_line(status: EngineStatus, *, now: datetime | None = None) -> str:
    """`<engine> off|idle|busy|error`, a busy engine followed by its job's status line,
    or by `by=` alone when the work is outside the engine's queue. `resident` marks an
    engine that is not working but keeps a model loaded — it still blocks the others."""
    parts = [status.name, status.state]
    if status.job_id is not None:
        ref = job_ref(status.name, status.job_id)
        parts.append(status_line(ref, status.job, now=now) if status.job is not None else ref)
    parts += _fields(by=status.busy_by, detail=status.detail or None)
    if status.resident:
        parts.append("resident")
    if status.state in ("idle", "busy") and status.contract != CONTRACT_VERSION:
        parts.append(f"contract={_text(status.contract)}")  # 아직 계약에 맞추지 않은 엔진. 없으면 null
    return " ".join(parts)


# ── recipes ─────────────────────────────────────────────────────────────────
def required_inputs(schema: dict[str, Any]) -> list[str]:
    """Required param names. `a|b` is one of them, `a+b` is both together."""
    names = [str(name) for name in schema.get("required") or []]
    for keyword in ("anyOf", "oneOf"):
        options = schema.get(keyword) or []
        groups = ["+".join(option["required"]) for option in options
                  if isinstance(option, dict) and option.get("required")]
        if groups and len(groups) == len(options):
            names.append("|".join(groups))
    return names


def recipe_line(recipe: dict[str, Any]) -> str:
    parts = [str(recipe.get("name"))]
    required = required_inputs(recipe.get("paramsSchema") or {})
    if required:
        parts.append("required=" + ",".join(required))
    if recipe.get("needsReview"):
        parts.append("needsReview")
    return " ".join(parts)


def _kind(spec: dict[str, Any]) -> str:
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = "|".join(kind)
    if kind == "array":
        return f"array<{_kind(spec.get('items') or {})}>"
    if kind == "object" and spec.get("properties"):
        return "object{" + ",".join(spec["properties"]) + "}"
    return kind or ("" if "enum" in spec else "any")  # 보기만 있으면 enum= 필드가 말한다


def param_lines(recipe: dict[str, Any]) -> list[str]:
    """One line per top-level param: `param <name>[*] [<type>] [low..high] [enum=] [default=] [# description]`."""
    schema = recipe.get("paramsSchema") or {}
    required = set(schema.get("required") or [])
    lines = []
    for name, spec in (schema.get("properties") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        parts = ["param", f"{name}*" if name in required else name, *filter(None, [_kind(spec)])]
        if "minimum" in spec or "maximum" in spec:
            parts.append(f"{spec.get('minimum', '')}..{spec.get('maximum', '')}")
        if "enum" in spec:
            parts.append("enum=" + ",".join(_text(choice) for choice in spec["enum"]))
        if "default" in spec:
            parts += _fields(default=spec["default"])
        if spec.get("description"):
            parts.append(f"# {spec['description']}")
        lines.append(" ".join(parts))
    return lines


# ── jobs ────────────────────────────────────────────────────────────────────
def status_line(ref: str, job: dict[str, Any], *, now: datetime | None = None) -> str:
    state = job.get("state")
    stages = job.get("stages") or []
    fields: dict[str, object] = {"recipe": job.get("recipe")}
    if state in FINAL_STATES:
        fields["took"] = _duration(_seconds(job.get("startedAt"), job.get("finishedAt")))
        if state == "failed":
            fields["errorCode"] = job.get("errorCode")
            fields["stage"] = next((stage.get("name") for stage in stages if stage.get("state") == "failed"), None)
        findings = job.get("findings") or []
        to_check = _to_check(findings)
        fields["assets"] = len(job.get("assets") or [])
        fields["findings"] = len(to_check)
        fields["fail"] = sum(finding.get("severity") == "fail" for finding in to_check) or None
        fields["cleared"] = (len(findings) - len(to_check)) or None
    else:
        stage = next((stage for stage in stages if stage.get("state") == "running"), None)
        if stage is not None:
            fields["stage"] = stage.get("name")
            progress = stage.get("progress")
            fields["progress"] = f"{round(progress * 100)}%" if isinstance(progress, (int, float)) else None
        fields["elapsed"] = _duration(_seconds(job.get("startedAt"), now=now))
    return " ".join([ref, str(state), *_fields(**fields)])


def result_lines(ref: str, job: dict[str, Any], *, top: int = DEFAULT_TOP, everything: bool = False,
                 now: datetime | None = None) -> list[str]:
    """The status line, the job folder, the assets, and the top findings still to check
    (`fail` first). Asset and evidence paths are relative to the `dir` line."""
    lines = [status_line(ref, job, now=now), f"dir {_text(job.get('dir'))}"]
    if job.get("error"):
        lines.append(f"error {_text(str(job['error'])[:MAX_ERROR_CHARS])}")

    assets = sorted(job.get("assets") or [], key=lambda asset: asset.get("role") != "final")
    shown = assets if everything else assets[:MAX_ASSETS]
    for asset in shown:
        lines.append(" ".join(["asset", *(_text(asset.get(key)) for key in ("id", "kind", "role")),
                               *_fields(review=asset.get("review")), _text(asset.get("file"))]))
    if len(shown) < len(assets):
        lines.append(f"more assets={len(assets) - len(shown)}")

    findings = _to_check(job.get("findings") or [])
    shown = findings if everything else findings[:max(top, 0)]
    for finding in shown:
        evidence = finding.get("evidence") or []
        lines.append(" ".join([
            "finding", *(_text(finding.get(key)) for key in ("id", "severity", "code")),
            *_fields(where=finding.get("where"), asset=finding.get("assetId"), params=finding.get("params") or None,
                     evidence=",".join(map(str, evidence)) or None),
        ]))
    if len(shown) < len(findings):
        lines.append(f"more findings={len(findings) - len(shown)}")
    return lines


# ── errors ──────────────────────────────────────────────────────────────────
def error_line(error: ArkAgentError) -> str:
    return " ".join(["error", error.code, *_fields(**error.params)])
