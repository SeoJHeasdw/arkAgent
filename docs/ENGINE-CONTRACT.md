# 엔진 계약 (v0 초안)

arkAgent와 로컬 엔진(tts · music · assets)이 지키는 공통 계약이다. 기준은
`local-assets-engine`의 작업 API이며, 이 문서는 그중 **모든 엔진이 지켜야 하는 부분**만
정한다. 엔진 고유 API(보관함, 휴지통, 프리셋 등)는 계약 밖에 자유롭게 둔다.

## 원칙

- 엔진은 앱 없이 뜨는 로컬 서비스다. 앱 · CLI · arkAgent는 같은 서비스의 같은 대기열을 쓴다.
- 엔진은 한 번에 작업 하나만 실행한다.
- 쉬는 엔진은 모델 메모리를 쥐지 않는다. `currentJob`이 없으면 다른 엔진이나 arkAgent의
  모델이 그 메모리를 써도 된다.
- 자동 검사 통과와 사람의 승인은 다르다. `review`를 `approved`로 바꾸는 것은 사람뿐이며
  arkAgent와 엔진의 자동 검사는 이 값을 바꾸지 않는다.
- 엔진은 단계마다 걸린 시간과 최대 메모리를 기록한다. 장비를 바꿀 근거가 여기서 나온다.

## 전송

`127.0.0.1:<포트>`의 HTTP · JSON. 루프백 밖의 요청과 다른 출처(Origin)의 요청은 거절한다.

| 엔진 | 포트 |
| --- | --- |
| assets | 47831 (기존) |
| tts | 47832 (제안) |
| music | 47833 (제안) |

## API

| 요청 | 동작 |
| --- | --- |
| `GET /api/health` | `{ok, engine, version, contract, currentJob, outputDir}`. `contract`는 이 문서의 버전(`0`), `currentJob`은 실행 중인 작업 ID 또는 `null` |
| `GET /api/recipes` | 이 엔진이 받는 일의 목록. `{recipes: [{name, label, paramsSchema, needsReview}]}`. `paramsSchema`는 JSON Schema |
| `POST /api/jobs` | `{recipe, params}`를 검증해 대기열에 넣고 작업 기록을 201로 돌려준다. 입력 오류는 400, 지금 받을 수 없으면 409 |
| `GET /api/jobs/{id}` | 작업 기록. 없으면 404 |
| `GET /api/jobs?limit=100` | 최근 작업. `{jobs: [...]}`, 최신이 먼저 |
| `POST /api/jobs/{id}/cancel` | 대기 작업은 바로 취소, 실행 중이면 중지 요청. 작업 기록을 돌려준다 |
| `POST /api/jobs/{id}/assets/{assetId}/review` | `{status}`: `pending`, `approved`, `rejected`. 사람의 판단을 기록하고 작업 기록을 돌려준다. 모르는 값은 400, 없는 에셋은 404 |

## 작업 기록

```json
{
  "id": "20261014-103000-ab12",
  "recipe": "course-narration",
  "title": "CH01 · 레슨 02",
  "dir": "/Users/me/local-tts-engine/output/jobs/20261014-103000-ab12",
  "params": {},
  "state": "done",
  "createdAt": "2026-10-14T10:30:00+09:00",
  "startedAt": "2026-10-14T10:30:02+09:00",
  "finishedAt": "2026-10-14T10:41:50+09:00",
  "stages": [
    { "name": "voice", "label": "목소리 생성", "state": "done", "progress": 1.0,
      "seconds": 640.2, "peakMemoryBytes": 6200000000 }
  ],
  "assets": [
    { "id": "a01", "kind": "audio", "role": "final", "file": "audio/track.wav", "review": "pending" }
  ],
  "findings": [
    { "id": "f01", "severity": "warn", "code": "pronunciation.term_unheard",
      "params": { "term": "standalone", "heard": "스탠들론" },
      "where": "7페이지 · 03:46", "assetId": "a01", "evidence": ["review/p07-standalone.wav"] }
  ],
  "error": null,
  "errorCode": null,
  "logTail": []
}
```

| 필드 | 값 |
| --- | --- |
| `dir` | 작업 폴더의 절대 경로. 결과를 열어 보거나 작업을 넘겨받는 쪽이 여기서 시작한다 |
| `state` | `queued`, `running`, `cancelling`, `done`, `failed`, `cancelled` |
| `stages[].state` | `running`, `done`, `failed`, `cancelled`, `skipped` |
| `stages[].seconds` · `peakMemoryBytes` | 단계가 끝나면 채운다. 모델을 올리는 시간이 따로 재어지면 `loadSeconds`로 나눠 적는다 |
| `assets[].file` · `findings[].evidence` | 작업 폴더(`dir`) 기준 상대 경로 |
| `assets[].review` | `pending`, `approved`, `rejected` |
| `findings[].severity` | `fail`(다시 만들어야 함), `warn`(사람이 확인) |
| `findings[].code` · `params` | 엔진이 정한 안정적인 식별자와 그 값. 표시 문구가 아니라 판정 근거다 |
| `findings[].where` | 사람이 그 자리를 찾아갈 수 있는 위치 |
| `errorCode` | 실패했을 때 `invalid_input`, `out_of_memory`, `interrupted`, `engine_error` 중 하나 |

`findings`는 사람이 확인할 곳의 목록이다. arkAgent는 이것을 줄여서 짧은 목록으로 전달할 뿐
새로 만들어 내지 않는다.

## 필수 레시피 `selftest`

모든 엔진은 모델을 올리지 않고 몇 초 안에 끝나는 `selftest` 레시피를 둔다. 계약 검사가 GPU
작업 없이 돌기 위한 것이다.

- `params`: `{steps?: number, stepSeconds?: number, failAt?: number}`. 기본은 2단계, 단계당 0.5초
- 단계를 차례로 실행하며 진행률을 갱신한다. 실행 중에는 `health.currentJob`이 이 작업이다.
- 파일 하나를 `assets`에, 항목 하나를 `findings`에 남긴다.
- 실행 중 `cancel`을 받으면 `cancelling`을 거쳐 `cancelled`로 끝난다.
- `failAt`(1부터 세는 단계 번호)이 주어지면 그 단계에서 `failed` · `errorCode: "engine_error"`로 끝난다.

## 계약 검사

실행 중인 엔진이 이 문서를 지키는지는 arkAgent의 검사 도구로 확인한다. `selftest`만 쓰므로
모델을 올리지 않는다.

```bash
uv run --project <arkAgent 경로> ark-engine-check http://127.0.0.1:47832
```

오류 응답의 본문은 `{detail}`이다.

## 엔진별 현재 상태와 맞출 것

| 엔진 | 지금 | 맞출 것 |
| --- | --- | --- |
| assets | 47831에 작업 API · 대기열 · 취소 · 단계별 시간과 최대 메모리 · `review`가 있다 | `health`에 `engine` · `contract`, `/api/recipes`, `findings`, `errorCode`, `selftest` |
| tts | HTTP가 없다. 작업 실행을 Electron 메인(`job-process.mjs`)이 맡고 Python은 CLI다 | 앱 없이 뜨는 작업 서비스를 만들고 앱이 그 클라이언트가 된다. 검수 결과를 `findings`로, 청취 승인을 `review`로 옮긴다. 발음 제안 사전은 레시피 `params`로 받는다 |
| music | CLI와 프로젝트 폴더, 배치 작업 ID · 이어하기 · 복구가 있다. HTTP 작업 API는 없다 | 기존 작업 기록을 감싸는 작업 서비스. 청취 판단을 `review`로 옮긴다 |

세 엔진 모두 "쉬는 엔진은 모델 메모리를 쥐지 않는다"를 지키는지 확인이 필요하다.

## 아직 정하지 않은 것

- **엔진 사이의 대기열.** v0에서는 arkAgent가 모든 엔진의 `health`를 보고 한 번에 한 엔진만
  일하게 한다. 사람이 앱에서 직접 넣은 작업과의 경합은 `currentJob`으로만 보인다. 장비 전체를
  묶는 잠금이 필요한지는 실제로 부딪혀 본 뒤 정한다.
- **이어하기.** 중단된 작업을 잇는 공통 표현은 없다. 지금은 같은 입력으로 다시 넣으면 엔진이
  자기 캐시로 이어가는 것에 기댄다.
- **진행 알림.** 지금은 `GET /api/jobs/{id}`를 다시 읽는다. 이벤트 스트림은 필요해지면 더한다.
