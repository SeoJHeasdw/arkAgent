"""`ark` puts every engine behind one surface and lets one engine work at a time.

The machine here is fake engines on one shared clock; an engine that was not
started refuses connections, as a closed port does.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from arkagent.engines.client import EngineClient
from arkagent.gateway.cli import EXIT_BUSY, EXIT_FAILED, EXIT_INPUT, EXIT_OFF, EXIT_OK, EXIT_TIMEOUT, main
from arkagent.gateway.core import EngineStatus, Gateway
from arkagent.gateway.lines import engine_line, result_lines, status_line
from fake_engine import FakeClock, FakeEngine

ADDRESSES = {"assets": "http://127.0.0.1:1", "tts": "http://127.0.0.1:2", "music": "http://127.0.0.1:3"}
LONG = '{"stepSeconds": 100}'


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class Machine:
    def __init__(self, *started: str, **flaws: str) -> None:
        self.time = FakeClock()
        self.engines = {name: FakeEngine(*filter(None, [flaws.get(name)]), name=name, time=self.time)
                        for name in started}
        self.handlers = {}  # 엔진 이름 → 가짜 엔진 대신 답할 함수
        self.gateway = Gateway(ADDRESSES, connect=self._connect, poll=1.0, sleep=self.time.sleep,
                               clock=self.time.clock)

    def _connect(self, url: str) -> EngineClient:
        name = next(name for name, address in ADDRESSES.items() if address == url)
        if name in self.engines and name not in self.handlers:
            return self.engines[name].client(url)
        transport = httpx.MockTransport(self.handlers.get(name, _refuse))
        return EngineClient(url, http=httpx.Client(transport=transport, base_url=url))

    def ark(self, *argv: str) -> int:
        return main(list(argv), gateway=self.gateway)


@pytest.fixture
def said(capsys):
    """What the last command printed: (stdout lines, stderr lines)."""
    def read() -> tuple[list[str], list[str]]:
        captured = capsys.readouterr()
        return captured.out.splitlines(), captured.err.splitlines()
    return read


# ── engines ─────────────────────────────────────────────────────────────────
def test_engines_shows_one_line_each_off_idle_or_busy(said):
    machine = Machine("assets", "music")
    machine.ark("submit", "music", "selftest", "--params", LONG)
    said()

    assert machine.ark("engines") == EXIT_OK

    out, err = said()
    assert out[:2] == ["assets idle", "tts off"]
    assert out[2].startswith("music busy music:job-1 running recipe=selftest")
    assert len(out) == 3 and err == []


def test_engine_that_answers_with_an_error_is_shown_and_blocks_the_others(said):
    machine = Machine("assets")
    machine.handlers["tts"] = lambda request: httpx.Response(500, json={"detail": "boom"})

    assert machine.ark("engines") == EXIT_OK
    assert said()[0][1] == 'tts error detail="engine answered 500: boom"'

    # 상태를 읽지 못한 엔진은 일하는 중일 수 있다 — 쉬는 것이 확인돼야 넣는다.
    assert machine.ark("submit", "assets", "selftest") == EXIT_BUSY
    assert said() == ([], ['blocked tts error detail="engine answered 500: boom"'])
    assert machine.engines["assets"].jobs == {}


def test_engine_not_yet_on_the_contract_is_marked():
    assert engine_line(EngineStatus("assets", "idle", contract=None)) == "assets idle contract=null"
    assert engine_line(EngineStatus("assets", "idle", contract=0)) == "assets idle"
    assert engine_line(EngineStatus("tts", "busy", contract=0, job_id="j1")) == "tts busy tts:j1"


# ── recipes ─────────────────────────────────────────────────────────────────
NARRATION = {
    "name": "course-narration", "label": "강의 음성", "needsReview": True,
    "paramsSchema": {
        "type": "object",
        "required": ["script"],
        "anyOf": [{"required": ["voice"]}, {"required": ["refAudio", "refText"]}],
        "properties": {
            "script": {"type": "string", "description": "읽을 대본 파일"},
            "speed": {"type": "number", "minimum": 0.5, "maximum": 2.0, "default": 1.0},
            "voice": {"enum": ["warm", "calm"]},
            "pages": {"type": "array", "items": {"type": "integer"}},
            "mix": {"type": "object", "properties": {"gain": {"type": "number"}, "fade": {"type": "boolean"}}},
        },
    },
}


def test_recipes_lists_names_with_required_inputs(said):
    machine = Machine("tts")
    machine.engines["tts"].recipes.append(NARRATION)

    assert machine.ark("recipes", "tts") == EXIT_OK

    assert said()[0] == ["selftest", "course-narration required=script,voice|refAudio+refText needsReview"]


def test_recipes_with_a_name_lists_every_param(said):
    machine = Machine("tts")
    machine.engines["tts"].recipes.append(NARRATION)

    assert machine.ark("recipes", "tts", "course-narration") == EXIT_OK
    assert said()[0] == [
        "course-narration required=script,voice|refAudio+refText needsReview",
        "param script* string # 읽을 대본 파일",
        "param speed number 0.5..2.0 default=1.0",
        "param voice enum=warm,calm",
        "param pages array<integer>",
        "param mix object{gain,fade}",
    ]

    assert machine.ark("recipes", "tts", "course-narration", "--json") == EXIT_OK
    assert json.loads(said()[0][0]) == NARRATION["paramsSchema"]

    assert machine.ark("recipes", "tts", "nope") == EXIT_INPUT
    assert said() == ([], ['error recipe.unknown engine=tts recipe=nope known=["selftest","course-narration"]'])


# ── submit · status · result ────────────────────────────────────────────────
def test_submit_returns_a_reference_that_status_and_result_read(said):
    machine = Machine("assets")

    assert machine.ark("submit", "assets", "selftest", "--params", '{"steps": 2}') == EXIT_OK
    assert said()[0][0].startswith("assets:job-1 running recipe=selftest")

    machine.time.sleep(5)
    assert machine.ark("status", "assets:job-1") == EXIT_OK
    assert said()[0] == ["assets:job-1 done recipe=selftest took=5.0s assets=1 findings=1"]

    assert machine.ark("result", "assets:job-1") == EXIT_OK
    assert said()[0] == [
        "assets:job-1 done recipe=selftest took=5.0s assets=1 findings=1",
        "dir /tmp/assets-engine/jobs/job-1",
        "asset a01 text final review=pending final/selftest.txt",
        'finding f01 warn selftest.sample where=1단계 asset=a01 params={"step":1} evidence=final/selftest.txt',
    ]


def test_params_can_come_from_a_file(tmp_path, said):
    machine = Machine("assets")
    (tmp_path / "params.json").write_text('{"steps": 3, "stepSeconds": 1}', encoding="utf-8")

    assert machine.ark("submit", "assets", "selftest", "--params", f"@{tmp_path / 'params.json'}") == EXIT_OK

    assert machine.engines["assets"].jobs["job-1"]["params"] == {"steps": 3, "stepSeconds": 1}


def test_submit_is_refused_while_another_engine_works(said):
    machine = Machine("assets", "music")
    machine.ark("submit", "music", "selftest", "--params", LONG)
    said()

    assert machine.ark("submit", "assets", "selftest") == EXIT_BUSY

    out, err = said()
    assert out == [] and len(err) == 1
    assert err[0].startswith("blocked music busy music:job-1 running recipe=selftest")
    assert machine.engines["assets"].jobs == {}


def test_submit_to_the_working_engine_joins_its_queue(said):
    machine = Machine("assets", "music")
    machine.ark("submit", "music", "selftest", "--params", LONG)
    said()

    assert machine.ark("submit", "music", "selftest") == EXIT_OK

    assert said()[0] == ["music:job-2 queued recipe=selftest"]


def test_when_idle_waits_for_the_other_engine_then_submits(said):
    machine = Machine("assets", "music")
    machine.ark("submit", "music", "selftest", "--params", '{"steps": 2, "stepSeconds": 5}')
    said()

    assert machine.ark("submit", "assets", "selftest", "--when-idle") == EXIT_OK

    out, err = said()
    assert machine.time.now >= 10
    assert out[0].startswith("assets:job-1 running recipe=selftest")
    assert len(err) == 1 and err[0].startswith("waiting music busy music:job-1 running")


def test_when_idle_gives_up_at_the_timeout_without_submitting(said):
    machine = Machine("assets", "music")
    machine.ark("submit", "music", "selftest", "--params", LONG)
    said()

    assert machine.ark("submit", "assets", "selftest", "--when-idle", "--timeout", "3") == EXIT_TIMEOUT

    out, err = said()
    assert out == [] and machine.engines["assets"].jobs == {}
    assert err[-2].startswith("blocked music busy music:job-1") and err[-1] == "error wait.timeout seconds=3.0"


def test_wait_prints_the_reference_first_and_the_result_when_the_job_ends(said):
    machine = Machine("assets")

    assert machine.ark("submit", "assets", "selftest", "--wait") == EXIT_OK

    out, err = said()
    assert out[0].startswith("assets:job-1 running")
    assert out[1:] == [
        "assets:job-1 done recipe=selftest took=1.0s assets=1 findings=1",
        "dir /tmp/assets-engine/jobs/job-1",
        "asset a01 text final review=pending final/selftest.txt",
        'finding f01 warn selftest.sample where=1단계 asset=a01 params={"step":1} evidence=final/selftest.txt',
    ]
    assert err == []


def test_wait_on_a_job_that_fails_exits_nonzero_with_its_error(said):
    machine = Machine("assets")

    assert machine.ark("submit", "assets", "selftest", "--params", '{"failAt": 1}', "--wait") == EXIT_FAILED

    assert said()[0][1:] == [
        "assets:job-1 failed recipe=selftest took=1.0s errorCode=engine_error assets=0 findings=0",
        "dir /tmp/assets-engine/jobs/job-1",
        'error "selftest was told to fail"',
    ]


def test_wait_stops_at_the_timeout_and_leaves_the_job_running(said):
    machine = Machine("assets")

    assert machine.ark("submit", "assets", "selftest", "--params", LONG, "--wait", "--timeout", "5") == EXIT_TIMEOUT

    out, err = said()
    assert len(out) == 2 and all(line.startswith("assets:job-1 running") for line in out)
    assert err == ["error wait.timeout seconds=5.0"]
    assert machine.engines["assets"].jobs["job-1"]["state"] == "running"

    machine.time.sleep(200)
    assert machine.ark("result", "assets:job-1", "--wait") == EXIT_OK
    assert said()[0][0].startswith("assets:job-1 done")


def test_cancel_stops_a_running_job(said):
    machine = Machine("assets")
    machine.ark("submit", "assets", "selftest", "--params", LONG)
    said()

    assert machine.ark("cancel", "assets:job-1") == EXIT_OK
    assert said()[0][0].startswith("assets:job-1 cancelling recipe=selftest")

    assert machine.ark("result", "assets:job-1") == EXIT_FAILED
    assert said()[0][0].startswith("assets:job-1 cancelled recipe=selftest")


# ── what stops a command ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("argv", "status", "line"),
    [
        (["status", "job-1"], EXIT_INPUT, "error job.bad_ref ref=job-1"),
        (["status", "film:job-1"], EXIT_INPUT, 'error engine.unknown engine=film known=["assets","music","tts"]'),
        (["status", "assets:job-9"], EXIT_INPUT,
         'error engine.http_error status=404 detail="작업을 찾을 수 없습니다." engine=assets'),
        (["submit", "film", "selftest"], EXIT_INPUT, 'error engine.unknown engine=film known=["assets","music","tts"]'),
        (["submit", "assets", "no-such-recipe"], EXIT_INPUT,
         'error engine.http_error status=400 detail="받을 수 없는 요청입니다." engine=assets'),
        (["submit", "assets", "selftest", "--params", "[1]"], EXIT_INPUT, "error params.not_object"),
        (["submit", "music", "selftest"], EXIT_BUSY,
         'error engine.http_error status=409 detail="지금은 받을 수 없습니다." engine=music'),
        (["submit", "tts", "selftest"], EXIT_OFF, "error engine.unreachable engine=tts"),
        (["recipes", "tts"], EXIT_OFF, "error engine.unreachable engine=tts"),
        (["status", "tts:job-1"], EXIT_OFF, "error engine.unreachable engine=tts"),
    ],
)
def test_what_stops_a_command_is_one_stderr_line_and_an_exit_status(argv, status, line, said):
    machine = Machine("assets", "music", music="refuses_jobs")

    assert machine.ark(*argv) == status

    assert said() == ([], [line])


def test_unreadable_params_are_bad_input(said):
    machine = Machine("assets")

    assert machine.ark("submit", "assets", "selftest", "--params", "{steps: 2}") == EXIT_INPUT

    out, err = said()
    assert out == [] and err[0].startswith("error params.unreadable detail=")
    assert machine.engines["assets"].jobs == {}


# ── lines ───────────────────────────────────────────────────────────────────
def test_status_line_of_a_running_job_names_the_stage_and_progress():
    job = {"state": "running", "recipe": "course-narration", "startedAt": "2026-10-14T10:30:00+09:00",
           "stages": [{"name": "script", "state": "done", "progress": 1.0},
                      {"name": "voice", "state": "running", "progress": 0.416}]}
    now = datetime.fromisoformat("2026-10-14T10:32:05+09:00")

    assert status_line("tts:j1", job, now=now) == (
        "tts:j1 running recipe=course-narration stage=voice progress=42% elapsed=125s")
    assert status_line("tts:j1", {"state": "queued", "recipe": "course-narration"}) == (
        "tts:j1 queued recipe=course-narration")


def test_result_lists_the_top_findings_fail_first_and_counts_the_rest():
    job = {
        "state": "done", "recipe": "course-narration", "dir": "/Users/me/tts engine/jobs/j1",
        "startedAt": "2026-10-14T10:30:00+09:00", "finishedAt": "2026-10-14T10:41:42+09:00",
        "assets": [{"id": f"a{index:02d}", "kind": "audio", "role": "page", "file": f"pages/p{index:02d}.wav",
                    "review": "pending"} for index in range(1, 25)]
                  + [{"id": "a99", "kind": "audio", "role": "final", "file": "audio/track.wav", "review": "pending"}],
        "findings": [
            {"id": "f01", "severity": "warn", "code": "pronunciation.term_unheard", "where": "7페이지 · 03:46",
             "assetId": "a99", "params": {"term": "standalone", "heard": "스탠들론"},
             "evidence": ["review/p07-standalone.wav"]},
            {"id": "f02", "severity": "warn", "code": "pace.too_fast", "where": "9페이지", "params": {}, "evidence": []},
            {"id": "f03", "severity": "fail", "code": "audio.clipped", "where": "12페이지", "assetId": "a12",
             "params": {"peak": 1.0}, "evidence": []},
        ],
    }

    lines = result_lines("tts:j1", job, top=2)

    assert lines[:3] == [
        "tts:j1 done recipe=course-narration took=702s assets=25 findings=3 fail=1",
        'dir "/Users/me/tts engine/jobs/j1"',
        "asset a99 audio final review=pending audio/track.wav",
    ]
    assert lines[3:22] == [f"asset a{index:02d} audio page review=pending pages/p{index:02d}.wav"
                           for index in range(1, 20)]
    assert lines[22:] == [
        "more assets=5",
        'finding f03 fail audio.clipped where=12페이지 asset=a12 params={"peak":1.0}',
        'finding f01 warn pronunciation.term_unheard where="7페이지 · 03:46" asset=a99 '
        'params={"term":"standalone","heard":"스탠들론"} evidence=review/p07-standalone.wav',
        "more findings=1",
    ]
    assert len(result_lines("tts:j1", job, everything=True)) == 2 + 25 + 3


def test_gateway_reads_engine_addresses_from_what_it_is_given():
    gateway = Gateway({"assets": "http://127.0.0.1:1"}, connect=lambda url: FakeEngine(name="assets").client(url))

    assert [(status.name, status.state) for status in gateway.engines()] == [("assets", "idle")]
