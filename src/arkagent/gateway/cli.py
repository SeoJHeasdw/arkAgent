"""`ark` — the gateway a frontier model calls from a shell.

    ark engines
    ark recipes <engine> [<recipe>]
    ark submit <engine> <recipe> --params '<JSON>' [--when-idle] [--wait]
    ark status <engine>:<job>
    ark result <engine>:<job>
    ark cancel <engine>:<job>

Output is short lines of stable words and key=value fields (see `lines.py`).
What was asked for goes to stdout. What stopped the command goes to stderr as
`error <code> ...` or `blocked <engine line>`, with the exit status saying which.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..core.config import load_config
from ..core.errors import ArkAgentError, ValidationError
from ..core.paths import ArkAgentPaths
from ..engines.client import EngineError, EngineUnreachable
from .core import Blocked, EngineStatus, Gateway, WaitTimeout
from .lines import DEFAULT_TOP, engine_line, error_line, param_lines, recipe_line, result_lines, status_line

EXIT_OK = 0
EXIT_FAILED = 1  # 작업이 failed · cancelled로 끝났거나 엔진이 예상 밖의 오류를 냈다
EXIT_INPUT = 2  # 잘못된 인자, 모르는 엔진 · 레시피 · 작업, 엔진이 거절한 params
EXIT_BUSY = 3  # 다른 엔진이 일하는 중이라 넣지 않았다
EXIT_OFF = 4  # 엔진이 꺼져 있다
EXIT_TIMEOUT = 5  # --timeout이 지났다. 넣은 작업은 계속 돈다

EXIT_HELP = ("exit status: 0 ok · 1 job failed or cancelled, or engine error · 2 bad input · "
             "3 another engine is working, nothing submitted · 4 engine off · 5 timed out, a submitted job keeps running")


def _exit_status(error: ArkAgentError) -> int:
    if isinstance(error, EngineUnreachable):
        return EXIT_OFF
    if isinstance(error, EngineError):
        return {400: EXIT_INPUT, 404: EXIT_INPUT, 409: EXIT_BUSY}.get(error.status, EXIT_FAILED)
    return EXIT_INPUT if isinstance(error, ValidationError) else EXIT_FAILED


def _params(raw: str) -> dict[str, Any]:
    try:
        params = json.loads(Path(raw[1:]).read_text("utf-8") if raw.startswith("@") else raw)
    except (OSError, ValueError) as error:
        raise ValidationError(str(error), code="params.unreadable", params={"detail": str(error)}) from error
    if not isinstance(params, dict):
        raise ValidationError("params must be a JSON object", code="params.not_object")
    return params


def _report(ref: str, job: dict[str, Any], args: argparse.Namespace) -> int:
    print("\n".join(result_lines(ref, job, top=args.top, everything=args.all)))
    return EXIT_FAILED if job.get("state") in ("failed", "cancelled") else EXIT_OK


def _announce_wait(blockers: list[EngineStatus]) -> None:
    for status in blockers:
        print(f"waiting {engine_line(status)}", file=sys.stderr, flush=True)


def _engines(gateway: Gateway, args: argparse.Namespace) -> int:
    for status in gateway.engines():
        print(engine_line(status))
    return EXIT_OK


def _recipes(gateway: Gateway, args: argparse.Namespace) -> int:
    if args.recipe is None:
        for recipe in gateway.recipes(args.engine):
            print(recipe_line(recipe))
        return EXIT_OK
    recipe = gateway.recipe(args.engine, args.recipe)
    if args.json:
        print(json.dumps(recipe.get("paramsSchema"), ensure_ascii=False, separators=(",", ":")))
    else:
        print("\n".join([recipe_line(recipe), *param_lines(recipe)]))
    return EXIT_OK


def _submit(gateway: Gateway, args: argparse.Namespace) -> int:
    started = gateway.clock()
    ref, job = gateway.submit(args.engine, args.recipe, _params(args.params), when_idle=args.when_idle,
                              timeout=args.timeout, on_wait=_announce_wait)
    # 기다리다 셸의 제한 시간에 끊겨도 작업 참조는 남도록 먼저 내보낸다.
    print(status_line(ref, job), flush=True)
    if not args.wait:
        return EXIT_OK
    left = None if args.timeout is None else max(0.0, args.timeout - (gateway.clock() - started))
    return _report(ref, gateway.wait(ref, timeout=left), args)


def _status(gateway: Gateway, args: argparse.Namespace) -> int:
    print(status_line(args.job, gateway.job(args.job)))
    return EXIT_OK


def _result(gateway: Gateway, args: argparse.Namespace) -> int:
    job = gateway.wait(args.job, timeout=args.timeout) if args.wait else gateway.job(args.job)
    return _report(args.job, job, args)


def _cancel(gateway: Gateway, args: argparse.Namespace) -> int:
    print(status_line(args.job, gateway.cancel(args.job)))
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ark", description="One surface over the local engines (tts, music, assets).",
                                     epilog=EXIT_HELP)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, run: Callable[[Gateway, argparse.Namespace], int], help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help, description=help, epilog=EXIT_HELP)
        sub.set_defaults(run=run)
        return sub

    def waits(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--wait", action="store_true", help="wait until the job ends, then print its result")
        sub.add_argument("--timeout", type=float, metavar="SECONDS",
                         help="stop waiting after this long (exit 5); the job itself keeps running")
        sub.add_argument("--top", type=int, default=DEFAULT_TOP, metavar="N",
                         help=f"findings to list, fail first (default {DEFAULT_TOP})")
        sub.add_argument("--all", action="store_true", help="list every asset and finding")

    command("engines", _engines, "one line per registered engine: off, idle, or busy with its running job")

    recipes = command("recipes", _recipes, "recipe names and required inputs; with a recipe, every param")
    recipes.add_argument("engine")
    recipes.add_argument("recipe", nargs="?")
    recipes.add_argument("--json", action="store_true", help="with a recipe: its raw paramsSchema")

    submit = command("submit", _submit, "queue a job and print <engine>:<job id>; refused while another engine works")
    submit.add_argument("engine")
    submit.add_argument("recipe")
    submit.add_argument("--params", default="{}", help="JSON object, or @path to a JSON file")
    submit.add_argument("--when-idle", action="store_true",
                        help="wait until no other engine is working, then submit")
    waits(submit)

    command("status", _status, "one line about a job").add_argument("job", help="<engine>:<job id>")

    result = command("result", _result, "state, job folder, assets, and the top findings to check")
    result.add_argument("job", help="<engine>:<job id>")
    waits(result)

    command("cancel", _cancel, "cancel a queued or running job").add_argument("job", help="<engine>:<job id>")
    return parser


def main(argv: list[str] | None = None, *, gateway: Gateway | None = None) -> int:
    args = _parser().parse_args(argv)
    own = gateway is None
    if gateway is None:
        gateway = Gateway(load_config(ArkAgentPaths.from_env()).engines)
    try:
        return args.run(gateway, args)
    except Blocked as blocked:
        for status in blocked.blockers:
            print(f"blocked {engine_line(status)}", file=sys.stderr)
        return EXIT_BUSY
    except WaitTimeout as timeout:
        if timeout.ref is not None and timeout.record is not None:
            print(status_line(timeout.ref, timeout.record))
        for status in timeout.blockers:
            print(f"blocked {engine_line(status)}", file=sys.stderr)
        print(error_line(timeout), file=sys.stderr)
        return EXIT_TIMEOUT
    except ArkAgentError as error:
        print(error_line(error), file=sys.stderr)
        return _exit_status(error)
    except httpx.HTTPError as error:  # 연결은 됐지만 응답이 끊기거나 늦었다
        print(f"error engine.transport type={type(error).__name__}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        # 기다림만 그만둔다. 넣은 작업은 엔진의 대기열 것이라 취소하지 않는다.
        return 130
    finally:
        if own:
            gateway.close()


if __name__ == "__main__":
    sys.exit(main())
