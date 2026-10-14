# 현재 상태

갱신: 2026-10-14. 지금 있는 것과 다음 작업만 둔다.

## 있는 것

- `engines/client.py` — 엔진 계약을 말하는 HTTP 클라이언트.
- `engines/conformance.py` — 계약 검사 도구 `ark-engine-check`(11개 검사). `selftest`만 써서 모델을
  올리지 않고, 엔진이 일하는 중이면 검사를 시작하지 않는다(종료 코드 3). 개정 전 판은 assets와 tts
  세션이 실제 엔진에 돌려 10/10을 보고했다. 개정 판은 가짜 엔진으로만 검증했다.
- `gateway/` — 관문 CLI `ark`. 프론티어가 Bash로 부르는 표면이다. `core.py`가 엔진들을 한
  표면으로 묶고(한 번에 한 엔진), `lines.py`가 출력 줄을 만들고, `cli.py`가 명령과 종료 코드를
  맡는다. MCP 표면은 `core.py`와 `lines.py`를 그대로 쓴다.
  - `ark engines` · `ark recipes <엔진> [<레시피>]` · `ark submit <엔진> <레시피> --params <JSON|@파일>
    [--when-idle] [--wait] [--timeout 초]` · `ark status|result|cancel <엔진>:<작업 ID>`.
  - 출력은 고정된 낱말과 `key=value`의 짧은 줄이다. 결과는 stdout, 멈춘 이유는 stderr의
    `error <코드> …` · `blocked <엔진 줄>`이다. 종료 코드는 `ark --help` 아래에 있다.
  - 다른 엔진이 `busy`이거나 상태를 읽지 못하면(`error`) 넣지 않는다. 같은 엔진의 실행 중인 작업은
    막지 않고 그 엔진의 대기열에 선다. 쉼 판정은 `health.busy`로 하고, 그 필드가 없는 개정 전
    엔진은 `currentJob`으로 판정한다.
  - `result`는 사람이 확인을 마친(`cleared`) 항목을 목록에서 빼고 개수만 센다.
  - `--wait`가 시간 제한이나 셸 종료로 끊겨도 작업은 취소하지 않는다. 작업 참조를 먼저 내보낸다.
  - 엔진 주소는 `~/.arkagent/config.yaml`의 `engines`(이름 → 주소 · 포트 · `null`은 제외)이고
    기본값은 계약 문서의 포트다.
  - 실제 assets 엔진에는 읽기 명령(`engines`, `recipes`, 없는 작업의 `status`)만 돌려 봤다.
    `submit` · `result` · `cancel`은 가짜 엔진으로만 검증했다.
- [ENGINE-CONTRACT.md](ENGINE-CONTRACT.md) — v0 초안. 기준은 `local-assets-engine`의 작업 API다.
  2026-10-14에 assets · tts 1차 보고의 제안을 반영해 개정했다(문서 끝의 변경 기록).
- `core/`, `providers/` — `rice`에서 옮긴 설정 · 오류 · 로컬 모델 연결(Ollama, OpenAI 호환).
  지금은 쓰는 곳이 없다. 하급 관리자 단계를 위한 것이다.
- `jobs/contract.py` — 프론티어와 로컬이 작업 소유권을 주고받는 계약 초안. 같은 이유로 쓰는
  곳이 없다.

## 다른 저장소에서 진행 중

세 엔진을 계약에 맞추는 일은 각 엔진 저장소의 세션이 한다. assets는 전부, music과 tts는
1단계(검사 통과)까지가 범위다. 그 보고에 계약 수정 제안이 오면 계약 문서와 검사 도구를 함께
고친다.

| 엔진 | 상태 (2026-10-14) | 다음 |
| --- | --- | --- |
| assets | 개정 검사 11/11(그 세션의 보고). 그 세션의 질문 둘에 답해 계약을 고쳤다(변경 기록 끝) | 커밋 · 푸시는 사용자가 직접 한다. 단계 안 입력 오류를 `invalid_input`으로 가르는 일은 엔진 고유로 남았다 |
| tts | 개정 검사 11/11(그 세션의 보고). 2단계 레시피 `course-narration` · `page-regenerate` · `probe-term`이 들어왔다. 실제 모델 실행과 사람의 청취는 아직 없다. 보고서: `local-tts-engine/output/reviews/2026-10-14/engine-contract/1단계 보고서.md`(6절) | 커밋 · 푸시는 사용자가 직접 한다. 첫 실제 실행(예: `probe-term` 한 용어 → `course-narration` 레슨 하나)은 사용자 확인 뒤 |
| music | **보류** — 그 저장소가 대규모 보정 작업 중이다. 계약 작업을 시작하지 않았다 | 작업량이 커서 2026-10-14 오후 또는 한참 뒤에, 이 세션과 연결되지 않은 새 세션이 한다(아래) |

assets와 tts는 개정 계약에 맞췄다. 공통 계약 작업은 music이 맞출 때까지 끝나지 않은 것으로 둔다.

**music을 맡는 세션에게.** 처음부터 개정된 계약에 맞춘다 — [ENGINE-CONTRACT.md](ENGINE-CONTRACT.md)를
끝의 변경 기록까지 읽고, assets · tts가 정한 해석(변경 기록의 "질문에 답해 고친 것")도 따른다. 앱 없이
뜨는 작업 서비스를 47833에 두고 `uv run --project <arkAgent 경로> ark-engine-check http://127.0.0.1:47833`
11/11을 목표로 한다. 계약과 맞지 않는 곳은 고치지 말고 제안으로 올린다. 받는 쪽은 그때 arkAgent
저장소에서 열린 세션이며, 계약 문서와 검사 도구를 함께 고친다.

## 다음 작업

1. **측정 기록.** 작업이 끝나면 엔진 · 레시피 · 단계별 시간 · 최대 메모리 · 실패 분류 · 장비 이름을
   `~/.arkagent/ledger.jsonl`에 한 줄로 남긴다. 장비를 바꿀 근거가 여기에 쌓인다. 지금 `ark`가
   끝난 작업 기록을 손에 쥐는 자리는 `--wait`와 `result`뿐이다. 기다리지 않고 넣은 작업을 어디서
   적을지부터 정한다.
2. **프론티어 비용 측정.** Claude Code 세션 기록에서 턴 수와 턴당 맥락 크기를 재는 도구.
   2026-10-14에 임시 스크립트로 잰 값은 tts 엔진 저장소 27세션 5,243턴, 턴당 약 33만 토큰,
   엔진 명령을 실행한 턴 39%다. 관문을 쓰기 전과 후를 같은 방법으로 비교한다.
3. **MCP 표면.** CLI와 같은 코어를 MCP 도구로도 낸다. 도구 수는 적게 둔다.
4. **첫 작업.** 강의 CH01의 한 레슨 음성을 관문으로 만들고 프론티어 턴 수를 잰다.

## 정하지 않은 것

- 이름을 arkAgent로 둘지.
- 데스크톱 셸(Electron · Tauri · Swift)과 관제 화면의 형태.
- Google 커넥터.
- 구독 상태에서 arkAgent가 프론티어를 부르는 실제 경로(하급 관리자 단계의 일).
