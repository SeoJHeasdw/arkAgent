# 엔진 계약 (v0 초안)

arkAgent와 로컬 엔진(tts · music · assets)이 지키는 공통 계약이다. 기준은
`local-assets-engine`의 작업 API이며, 이 문서는 그중 **모든 엔진이 지켜야 하는 부분**만
정한다. 엔진 고유 API(보관함, 휴지통, 프리셋, 일시정지 등)는 계약 밖에 자유롭게 둔다.

초안인 동안 `contract`는 `0`으로 두고, 바뀐 것은 맨 아래 [변경 기록](#변경-기록)에 적는다.

## 원칙

- 엔진은 앱 없이 뜨는 로컬 서비스다. 엔진에 작업을 넣는 경로(HTTP와 그것을 쓰는 앱 · CLI ·
  arkAgent)는 같은 서비스의 같은 대기열을 쓴다. 손으로 돌리는 기존 CLI처럼 대기열을 거치지 않는
  실행은 계약 밖이지만, 엔진이 알 수 있으면 그동안 `health.busy`로 알린다.
- 엔진은 한 번에 작업 하나만 실행한다.
- 쉬는 엔진은 모델 메모리를 쥐지 않는다. 엔진 서비스 프로세스는 모델 런타임을 올리지 않고,
  모델은 단계 프로세스에서만 올라간다. `busy`가 거짓이면 다른 엔진이나 arkAgent의 모델이 그
  메모리를 써도 된다. 상주 모델 서버처럼 이를 지키지 못하는 동안에는 숨기지 않고 `busy` ·
  `resident`로 알린다. 목표는 서비스가 모델 서버의 수명을 맡아(작업이 오면 켜고, 대기열이 비면 짧은
  유예 뒤 끈다) 원칙을 지키는 것이다.
- 자동 검사 통과와 사람의 승인은 다르다. `assets[].review`와 `findings[].review`를 바꾸는 것은
  사람뿐이며 arkAgent와 엔진의 자동 검사는 이 값을 바꾸지 않는다.
- 사람의 승인은 그 파일에 묶인다. 승인한 뒤 파일이 바뀌면 그 승인은 무효다.
- 엔진은 단계마다 걸린 시간과 최대 메모리를 기록한다. 장비를 바꿀 근거가 여기서 나온다.

## 전송

`127.0.0.1:<포트>`의 HTTP · JSON. 루프백 밖의 요청과 다른 출처(Origin)의 요청은 거절한다.
오류 응답의 본문은 `{detail}`이다.

| 엔진 | 포트 |
| --- | --- |
| assets | 47831 (기존) |
| tts | 47832 (제안) |
| music | 47833 (제안) |

## API

| 요청 | 동작 |
| --- | --- |
| `GET /api/health` | `{ok, engine, version, contract, busy, busyBy?, resident?, currentJob, outputDir}` (아래) |
| `GET /api/recipes` | 이 엔진이 받는 일의 목록. `{recipes: [{name, label, paramsSchema, needsReview}]}` (아래) |
| `POST /api/jobs` | `{recipe, params}`를 검증해 대기열에 넣고 작업 기록을 201로 돌려준다. 입력 오류는 400, 지금 받을 수 없으면 409 |
| `GET /api/jobs/{id}` | 작업 기록. 없으면 404 |
| `GET /api/jobs?limit=100` | 최근 작업. `{jobs: [...]}`, 최신이 먼저 |
| `POST /api/jobs/{id}/cancel` | 대기 작업은 바로 취소, 실행 중이면 중지 요청. 작업 기록을 돌려준다 |
| `POST /api/jobs/{id}/assets/{assetId}/review` | `{status}`: `pending`, `approved`, `rejected`. 사람의 판단을 기록하고 작업 기록을 돌려준다. 모르는 값은 400, 없는 에셋은 404 |
| `POST /api/jobs/{id}/findings/{findingId}/review` | `{status}`: `pending`, `cleared`. 사람이 그 자리를 확인했음을 기록하고 작업 기록을 돌려준다. 모르는 값은 400, 없는 항목은 404 |

### health

| 필드 | 값 |
| --- | --- |
| `contract` | 이 문서의 버전(`0`) |
| `busy` | 이 엔진이 지금 장비의 메모리를 쥐고 있는가. 일 때문이든(대기열의 작업, 앱이 직접 돌리는 작업, 일시정지한 채 모델을 쥔 작업) 쉬는 동안 올라 있는 상주 모델 때문이든 참이다. **쉼 판정은 이 값으로 한다** |
| `busyBy` | 선택. `busy`일 때 무엇이 메모리를 쥐고 있는지 사람이 알아볼 짧은 이름(예: `app:15100`, `lane:generation.lock`, `ace-step:18001`). 작업 ID가 아니다 |
| `resident` | 선택(없으면 거짓). `busy`인 까닭이 일이 아니라 쉬는 동안에도 올라 있는 상주 모델뿐이면 참. 이때 `currentJob`은 `null`이다. 일이 시작되면 거짓으로 돌아간다 |
| `currentJob` | 실행 중인 작업의 ID, 없으면 `null`. **이 엔진의 작업 기록에 있는 작업**(`GET /api/jobs/{id}`로 읽고 `cancel`할 수 있는 작업)만 적는다. 대기열을 거치지 않았어도 같은 작업 저장소에 기록이 있으면 적는다(예: 엔진 서버 없이 도는 독립 CLI의 작업). 작업 기록이 없는 일은 `busyBy`로만 알린다 |
| `outputDir` | 작업 기록이 있는 곳. 작업 폴더(`dir`)는 이 밖에 있어도 된다 |

`currentJob`이 있으면 `busy`는 참이다. `busy`가 참이고 `currentJob`이 `null`이면 작업 기록이 없는 일을
하는 중이거나(`resident`가 거짓), 상주 모델을 쥔 채 쉬는 중이다(`resident`가 참). arkAgent는 어느
쪽이든 메모리가 쥐어져 있으므로 다른 엔진을 막는다.

### recipes

- `paramsSchema`는 JSON Schema(2020-12)다. draft-07과 겹치는 키워드(`if`/`then`/`else`, `const`,
  `anyOf` 등)로 쓰고 배열 튜플(`items` 배열, `prefixItems`)은 쓰지 않는다.
- 스키마는 입력의 정석 형태를 말한다. 최종 판단은 엔진의 검증(`POST /api/jobs`의 400)이다. 엔진은
  스키마보다 너그러워도 되고(문자열로 쓴 숫자, 모르는 키 무시), 다른 필드에 따라 달라지는 한계처럼
  스키마로 말할 수 없는 것은 `description`에 적는다.
- `needsReview`: 이 레시피의 결과는 사람이 보고 판단해야 쓸 수 있다. arkAgent는 `needsReview`인
  레시피의 결과를 사람의 승인 없이 최종으로 넘기지 않는다.

### 이어하기 `params.resumeFrom`

- 끊긴 작업을 잇는 레시피는 `params.resumeFrom`으로 같은 엔진의 작업 ID를 받는다. 받는 레시피만
  `paramsSchema`에 이 키를 둔다.
- 엔진은 그 작업의 결과를 스스로 검사해(계보 · 지문 · 해시 등) 통과한 부분만 이어 쓰고, 이어 쓴
  단계는 `skipped`로 남긴다. 검사를 통과하지 못하면 그 부분은 다시 만든다.
- 묵시적 재사용은 요구하지 않는다. `resumeFrom` 없이 같은 입력으로 다시 넣으면 처음부터 다시
  계산해도 된다.
- arkAgent는 `interrupted`로 끝난 작업을 다시 넣을 때, 레시피가 `resumeFrom`을 받으면 그것을 붙인다.

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
      "where": "7페이지 · 03:46", "assetId": "a01", "evidence": ["review/p07-standalone.wav"],
      "review": "pending" }
  ],
  "error": null,
  "errorCode": null,
  "logTail": []
}
```

| 필드 | 값 |
| --- | --- |
| `dir` | 작업 폴더의 절대 경로. 결과를 열어 보거나 작업을 넘겨받는 쪽이 여기서 시작한다. 파일에 적지 않고 읽을 때 붙여도 된다. 여러 작업이 한 폴더를 나눠 쓸 수 있고(예: 같은 곡의 버전 추가 · 구간 수정), `outputDir` 밖이어도 된다. 나눠 쓰는 폴더는 작업 하나를 정리한다고 지우지 않는다 |
| `state` | `queued`, `running`, `paused`, `cancelling`, `done`, `failed`, `cancelled` (아래) |
| `stages[].state` | `running`, `done`, `failed`, `cancelled`, `skipped` |
| `stages[].seconds` | 단계가 끝나면 채운다. 모델을 올리는 시간이 따로 재어지면 `loadSeconds`로 나눠 적는다 |
| `stages[].peakMemoryBytes` | 단계 동안 그 단계의 일을 한 프로세스가 쓴 최대 메모리. macOS에서는 `/usr/bin/time -l`의 peak memory footprint(통합 메모리의 Metal 버퍼가 포함된다). 단계 밖에서 오래 사는 모델 서버가 일을 했으면 단계 동안 그 프로세스의 footprint를 일정 간격으로 읽은 최댓값을 적는다(쉬는 동안 쥔 양도 포함된다). 프로세스 여럿이 동시에 돌면 각 최대값의 합(상한)으로 적는다. 다 세지 못하면 센 만큼 적고, 모르거나 프로세스가 없는 단계는 `null` |
| `stages[].peakMemoryMethod` | 선택. `peakMemoryBytes`를 잰 방식. `time-l`(단계 프로세스의 `/usr/bin/time -l`) 또는 `sampled`(단계 동안 표본으로 읽은 footprint의 최댓값). 측정 기록이 장비를 견줄 때 같은 방식끼리 견주기 위한 것이다 |
| `assets[].file` · `findings[].evidence` | 작업 폴더(`dir`) 기준 상대 경로. `..`을 쓰지 않는다 |
| `assets[].review` | `pending`, `approved`, `rejected`. `pending`은 "사람의 판단이 기록되지 않았다"는 뜻이지 "아직 안 봤다"가 아니다(들어 보기만 한 것도 `pending`). 앱이 사람의 판단을 다른 방식으로 적는다면(즐겨찾기처럼) 뜻이 같을 때만 이 값으로 옮긴다 |
| `assets[].reviewedSha256` | 선택. 사람이 판단할 때의 파일 SHA-256(소문자 16진수 64자). 지금 파일과 다르면 엔진은 그 판단을 내보내지 않고 `pending`으로 돌린다 |
| `findings[].severity` | `fail`(다시 만들어야 함), `warn`(사람이 확인) |
| `findings[].code` · `params` | 엔진이 정한 안정적인 식별자와 그 값. 표시 문구가 아니라 판정 근거다 |
| `findings[].where` | 사람이 그 자리를 찾아갈 수 있는 위치 |
| `findings[].review` | `pending`, `cleared`. 사람이 그 자리를 확인했으면 `cleared`다. 새 항목은 `pending` |
| `errorCode` | `failed`일 때만 채운다. 아래 표 |

`findings`는 사람이 확인할 곳의 목록이다. arkAgent는 이것을 줄여서 짧은 목록으로 전달할 뿐
새로 만들어 내지 않는다.

### 작업 상태

| 상태 | 뜻 |
| --- | --- |
| `paused` | 사람이 멈춘 작업. 끝난 상태가 아니다. 멈추고 잇는 API는 엔진 고유이며 arkAgent는 쓰지 않는다. 멈춘 채 모델 메모리를 쥐고 있으면(예: SIGSTOP) `busy`는 참이고, 프로세스를 내렸으면 거짓이다 |
| `cancelled` | **취소 요청**(`POST …/cancel`, 앱의 취소 단추)으로 끝난 작업에만 쓴다 |
| `failed` | 그 밖의 이유로 끝나지 못한 작업. 엔진이 멈추거나 다시 시작하면서 끝낸 작업도 여기다(`interrupted`) |

엔진이 다시 시작한 뒤 대기 작업은 이어서 실행하거나 `failed` · `interrupted`로 끝낸다. 어느 쪽이든
`cancelled`로 두지 않는다. 다만 엔진이 멈출 때 이미 취소 요청을 받아 `cancelling`이던 작업은
`cancelled`로 닫는다. 사람이 취소한 작업과 엔진이 끊은 작업을 arkAgent가 구별해야, 끊긴 작업만
다시 넣는다(레시피가 받으면 `resumeFrom`을 붙여).

### errorCode

| 값 | 쓰는 때 |
| --- | --- |
| `invalid_input` | 받은 뒤에 드러난 입력 문제(예: 편집을 지원하지 않는 원본). 제출할 때 알 수 있는 입력 오류는 400으로 거절해 기록이 생기지 않는다 |
| `out_of_memory` | 메모리 부족의 근거가 있을 때. 엔진이 보내지 않은 SIGKILL, 런타임의 메모리 할당 실패(예: MLX `[metal::malloc]`, Metal `Insufficient Memory`를 재시도한 뒤에도 실패) 등. 사람이 `kill -9`한 것과 SIGKILL은 구별되지 않는다(알려진 한계) |
| `interrupted` | 엔진이 멈추거나 다시 시작하면서 끊긴 작업 |
| `engine_error` | 나머지 |

`errorCode`는 엔진의 판단이다. 판단의 근거(신호, 런타임의 오류 문구)를 `error`에 남긴다. 엔진이 가를
수 없으면 `engine_error`로 둔다. 단계 프로세스가 엔진에 오류의 종류를 알리는 방식(종료 코드, 출력 줄
등)은 엔진 고유이며 계약이 정하지 않는다.

## 필수 레시피 `selftest`

모든 엔진은 모델을 올리지 않고 몇 초 안에 끝나는 `selftest` 레시피를 둔다. 계약 검사가 GPU
작업 없이 돌기 위한 것이다.

- `params`: `{steps?: integer, stepSeconds?: number, failAt?: integer}`. 기본은 2단계, 단계당 0.5초
- 단계를 차례로 실행하며 진행률을 갱신한다. 실행 중에는 `health.currentJob`이 이 작업이고
  `health.busy`는 참이다.
- 파일 하나를 `assets`에, 항목 하나를 `findings`에 남긴다.
- 실행 중 `cancel`을 받으면 `cancelling`을 거쳐 `cancelled`로 끝난다.
- `failAt`(1부터 세는 단계 번호)이 주어지면 그 단계에서 `failed` · `errorCode: "engine_error"`로 끝난다.
- `selftest` 작업은 결과물이 아니다. 엔진은 그 기록과 폴더를 언제든 지워도 된다(예: 시작할 때 정리).
  보관함, 측정 집계, 완료 알림에서 빼도 된다.

## 계약 검사

실행 중인 엔진이 이 문서를 지키는지는 arkAgent의 검사 도구로 확인한다. `selftest`만 쓰므로
모델을 올리지 않는다.

```bash
uv run --project <arkAgent 경로> ark-engine-check http://127.0.0.1:47832
```

- 엔진이 일하는 중(`currentJob`이 있거나, `busy`인데 `resident`가 아님)이면 검사를 시작하지 않고
  `BUSY`를 알리며 종료 코드 3으로 끝난다. `selftest`가 실제 작업 뒤에 줄을 서서 시간 초과로 실패하지
  않게 하려는 것이다. 상주 모델만 떠 있는 엔진은 검사한다 — `selftest`는 모델을 쓰지 않는다.
- 한 번 돌 때마다 `selftest` 작업 셋이 남는다. 지울지는 엔진이 정한다(위).

## 엔진별 현재 상태와 맞출 것

2026-10-14 기준. 각 엔진 세션의 보고에서 옮겼다.

| 엔진 | 지금 | 맞출 것 |
| --- | --- | --- |
| assets | 개정 계약 검사 11/11 통과(그 세션의 보고, 커밋 전). `busy`는 기기 공용 생성 차선 잠금까지 본다. 멈춤 기능이 없어 `paused`는 쓰지 않고, 결과 파일을 고쳐 쓰지 않아 `reviewedSha256`도 쓰지 않는다. 진단은 별도 프로세스로 옮겨 쉬는 엔진이 50MB를 유지한다 | 단계 안에서 드러나는 입력 오류는 아직 `engine_error`(가르는 방식은 엔진 고유) |
| tts | 개정 계약 검사 11/11 통과(그 세션의 보고, 커밋 전). 레시피 `course-narration` · `page-regenerate` · `probe-term`(모두 `needsReview`)이 들어왔지만 실제 모델로 돌린 적은 없다. 앱이 일하면 `busy` · `busyBy: "app:<PID>"` · `currentJob: null`. 앱이 프로세스 없이 `paused`로 기다리는 동안은 `busy`가 거짓이지만 앱이 작업 차선을 쥐고 있어, 그 사이 tts에 넣은 작업은 `queued`로 기다린다(다른 엔진은 메모리를 써도 된다) | 첫 실제 실행은 사람의 확인 뒤. 그쪽 남은 일: `course-narration`의 자막, `out_of_memory` 분류, `resumeFrom` 이어하기, 서비스가 죽을 때 남는 단계 프로세스 정리, 앱을 서비스의 클라이언트로 |
| music | 1단계 계약 검사 11/11 통과(그 세션의 보고, 커밋 전). 작업 서비스 `uv run music-engine serve`(47833). 기기 공용 생성 차선으로 CLI · 앱과 겹치지 않는다(CLI가 생성 중이면 `busy` · `busyBy: "lane:generation.lock"`). `reviewedSha256`을 쓴다. 모델은 앱이 켜 두는 상주 ACE 서버(127.0.0.1:18001)에 있어 쉬는 동안 약 26GB를 쥔다 — 원칙을 지키지 못하는 상태다 | 상주 ACE만 떠 있을 때 `resident: true`를 내고 검사를 다시 돌린다. 2단계: 레시피 `song` · `versions` · `repaint` · `resume`(`resumeFrom`), 서비스가 ACE의 수명을 맡아 원칙을 지킨다, 상주 서버의 메모리는 `sampled`로 잰다 |

## 아직 정하지 않은 것

- **엔진 사이의 대기열.** v0에서는 arkAgent가 모든 엔진의 `health.busy`를 보고 한 번에 한 엔진만
  일하게 한다. 사람이 앱에서 직접 넣은 작업과의 경합은 `busy`로만 보인다. 장비 전체를 묶는 잠금이
  필요한지는 실제로 부딪혀 본 뒤 정한다. (tts는 엔진 안에서 앱과 서비스가 파일 잠금 하나를 나눠 쓴다.)
- **사람이 고른 최종본.** music의 최종본 선택, tts `page-regenerate`의 후보 고르기처럼 사람이 여러
  결과 중 하나를 고르는 일은 지금 엔진 고유로 둔다. arkAgent가 고른 결과를 다음 일에 넘겨야 할 때
  계약으로 올린다.
- **진행 알림.** 지금은 `GET /api/jobs/{id}`를 다시 읽는다. 이벤트 스트림은 필요해지면 더한다.

## 변경 기록

### 2026-10-14 개정 (assets · tts 1차 보고 반영)

엔진이 맞출 것:

1. `health.busy`(필수)와 `health.busyBy`(선택). `currentJob`에는 작업 ID만 넣는다.
2. `findings[].review`(`pending` · `cleared`, 새 항목은 `pending`)와
   `POST /api/jobs/{id}/findings/{findingId}/review`.
3. 엔진이 멈추며 끝낸 작업은 실행 중이든 대기 중이든 `failed` · `interrupted`. `cancelled`는 취소 요청에만.
4. 작업 상태 `paused`를 받아들인다(멈춤 기능이 있는 엔진만 쓴다).
5. `assets[].reviewedSha256`(선택) — 사람의 승인을 파일 해시에 묶는 엔진은 이것을 쓴다.
6. 검사 도구를 다시 돌린다. 개정된 검사는 `busy`, 항목 확인 기록을 함께 본다.

문서로만 정한 것: `needsReview` · `pending`의 뜻, `peakMemoryBytes`의 정의, `errorCode` 각 값의 근거,
`paramsSchema`의 방언과 "최종 판단은 엔진의 검증", `selftest`의 `steps` · `failAt`은 정수, `selftest`
작업은 지워도 된다, 대기열 원칙의 범위(작업을 넣는 경로만).

받아들이지 않은 것: 없음. 엔진 쪽 일로 남긴 것: tts 앱의 `rejected` 지원, assets의 진단 메모리(해결됨).

assets 반영 뒤 질문에 답해 고친 것(같은 날):

- `currentJob`은 "대기열 안의 작업"이 아니라 "작업 기록이 있는 작업"이다. 엔진 서버 없이 도는 독립
  CLI의 작업도 같은 작업 저장소에 기록이 있으면 적는다. 작업 기록이 없는 일만 `busyBy`로 알린다.
- 단계 프로세스가 입력 오류를 알리는 방식은 엔진 고유다. 가를 수 없으면 `engine_error`로 둔다.

tts 반영 보고에 따라 적은 것(같은 날): 엔진이 멈출 때 이미 `cancelling`이던 작업은 `cancelled`로 닫는다.

### 2026-10-14 music 1차 보고 반영 (같은 날)

엔진이 맞출 것:

1. `health.resident`(선택) — 상주 모델 서버를 쥔 채 쉬는 엔진만 쓴다. `busy`의 뜻을 "메모리를 쥐고
   있는가"로 분명히 했다. 검사 도구는 `resident`만인 엔진을 검사하고, `selftest`가 끝난 뒤 `busy`가
   남아도 `resident`면 통과시킨다.
2. 이어하기를 받는 레시피는 `params.resumeFrom`(같은 엔진의 작업 ID)으로 받는다. tts · music이 같은
   모양을 계획해 "아직 정하지 않은 것"에서 올렸다. 묵시적 재사용은 요구하지 않는다.
3. `stages[].peakMemoryMethod`(선택, `time-l` · `sampled`). 단계 밖의 상주 서버는 표본 최댓값으로 잰다.

문서로만 정한 것: `dir`는 여러 작업이 나눠 쓸 수 있고 `outputDir` 밖이어도 된다, `outputDir`는 작업
기록이 있는 곳, 들어 보기만 한 것도 `pending`.

엔진 고유로 둔 것: 사람이 고른 최종본(아직 정하지 않은 것으로 옮김).
