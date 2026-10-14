"""Configuration backed by `~/.arkagent/config.yaml`.

Plain dataclasses with explicit defaults so everything boots with no config file
present. YAML load/save is intentionally lazy — `load_config` falls back to
defaults if the file or parser is absent, which keeps `core` import-clean.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field, fields

from .paths import ArkAgentPaths

log = logging.getLogger("arkagent.core.config")


@dataclass(frozen=True)
class ProviderConfig:
    """Active LLM provider. API keys are NOT stored here — this only names the
    selection and its local-inference knobs."""

    kind: str = "ollama"  # "ollama" | "openai" (OpenAI-compatible, incl. local servers) | "echo"
    model: str = ""  # 비어 있으면 아직 고르지 않은 것 — 호출 전에 설정해야 한다
    # 도구 인자 같은 깊은 JSON 생성에서 높은 온도는 재시도 편차·괄호/인용부호
    # 실수의 온상이다. 0.5는 도구 정확도와 (로컬 thinking 모델의 저온 반복 루프를
    # 피하는) 안정성의 균형점이다. 구조화 JSON 전용 호출은 이 값과 무관하게
    # 호출부가 낮은 값으로 고정한다.
    temperature: float = 0.5
    base_url: str | None = None  # Ollama 서버, 또는 OpenAI 호환 엔드포인트
    # Per-generation OUTPUT cap (Ollama `num_predict` / OpenAI `max_tokens`).
    # Generous enough for long structured text AND large tool-call JSON, while
    # still capping runaway generation.
    max_output_tokens: int = 16_384
    # 로컬 추론 HTTP 타임아웃(초). 로컬 모델은 모델 로드와 긴 생성에서 60초를
    # 쉽게 넘긴다. 공식 OpenAI API는 이 값을 사용하지 않는다.
    timeout_seconds: int = 600
    # Ollama 요청 컨텍스트 창(num_ctx). 미지정 시 Ollama 서버 기본값(대개 4096)이
    # 적용돼 긴 요청에서 프롬프트 앞부분(시스템 프롬프트·도구 스키마)이 **소리 없이
    # 잘린다** — 도구 호출 실패·지시 무시의 근본 원인.
    num_ctx: int = 32_768
    # Ollama request keep_alive — 비정상 종료로 명시적 unload를 못 보냈을 때도
    # Ollama가 스스로 회수하는 crash-safety 상한. 정상 흐름은 아래 idle 유예 뒤
    # keep_alive=0 unload를 보내므로 이 시간만큼 계속 상주하지 않는다.
    keep_alive: str = "5m"
    # 마지막 활성 lease 후 곧바로 이어지는 호출이 이미 로드된 모델을 재사용할
    # 짧은 유예. 만료 시 /api/generate keep_alive=0으로 명시 해제한다.
    idle_unload_seconds: float = 60.0


# 등록된 엔진과 그 주소. docs/ENGINE-CONTRACT.md의 포트 표와 같아야 한다
# (tests/test_config.py가 둘을 대조한다).
DEFAULT_ENGINES: dict[str, str] = {
    "assets": "http://127.0.0.1:47831",
    "tts": "http://127.0.0.1:47832",
    "music": "http://127.0.0.1:47833",
}


@dataclass(frozen=True)
class Config:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    # 엔진 이름 → 주소. config.yaml의 `engines`는 기본값 위에 덮인다: 같은 이름은
    # 주소를 바꾸고, 새 이름은 엔진을 더하고, null은 그 엔진을 뺀다.
    engines: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ENGINES))

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigStore:
    """A mutable holder for the live `Config`.

    Components that must observe the *current* config (the provider registry)
    hold this store, not a captured `Config` snapshot, so an update is visible
    everywhere without re-wiring the graph.
    """

    def __init__(self, config: Config) -> None:
        self._config = config

    @property
    def config(self) -> Config:
        return self._config

    def update(self, config: Config) -> None:
        self._config = config


def load_config(paths: ArkAgentPaths) -> Config:
    """Load `config.yaml` over the defaults. Missing file or missing PyYAML →
    defaults.

    Fail-soft: 파손된 YAML, dict가 아닌 최상위 값, 미지 키 어느 것도 부팅을 막지
    않는다 — 경고를 남기고 기본값(또는 인식 가능한 부분만)으로 기동한다. 설정
    파일 오류로 아예 안 뜨는 것이 최악의 상태다.
    """
    cfg_path = paths.config
    if not cfg_path.exists():
        return Config()
    try:
        import yaml  # optional dependency
    except ImportError:
        return Config()
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — yaml.YAMLError + 인코딩/IO 오류
        log.warning("config.yaml is corrupted — booting with defaults", exc_info=True)
        return Config()
    if not isinstance(raw, dict):
        log.warning("config.yaml top level is %s (expected mapping) — booting with defaults", type(raw).__name__)
        return Config()
    return _merge(Config(), raw)


def save_config(paths: ArkAgentPaths, config: Config) -> None:
    """Persist to `config.yaml` atomically. No-op if PyYAML is unavailable.

    temp write → fsync → rename: 저장 도중 프로세스가 죽어도 config.yaml은
    이전 버전 전체 또는 새 버전 전체 중 하나다(반쪽 YAML 없음).
    """
    try:
        import yaml
    except ImportError:
        return
    paths.ensure()
    target = paths.config
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(yaml.safe_dump(config.to_dict(), allow_unicode=True))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


def _known_fields(cls, section: dict) -> dict:
    """dataclass가 모르는 키를 걸러낸다 — 수동 편집이 남긴 잔여 키가 TypeError로
    전체 로드를 무산시키지 않게(무시된 키는 경고만)."""
    names = {f.name for f in fields(cls)}
    unknown = set(section) - names
    if unknown:
        log.warning("config.yaml: ignoring unknown %s keys: %s", cls.__name__, sorted(unknown))
    return {k: v for k, v in section.items() if k in names}


def _merge(base: Config, raw: dict) -> Config:
    """Shallow-merge a parsed dict onto defaults, section by section."""

    def section(name: str) -> dict:
        value = raw.get(name)
        return value if isinstance(value, dict) else {}

    prov = {**asdict(base.provider), **_known_fields(ProviderConfig, section("provider"))}
    return Config(provider=ProviderConfig(**prov), engines=_merge_engines(base.engines, section("engines")))


def _merge_engines(base: dict[str, str], section: dict) -> dict[str, str]:
    engines = dict(base)
    for name, value in section.items():
        # 이름은 작업 참조 `<엔진>:<작업 ID>`의 앞부분이라 콜론과 공백을 받지 않는다.
        if not isinstance(name, str) or not name or ":" in name or any(ch.isspace() for ch in name):
            log.warning("config.yaml: ignoring engine with unusable name %r", name)
        elif value is None:
            engines.pop(name, None)
        elif isinstance(value, int) and not isinstance(value, bool):
            engines[name] = f"http://127.0.0.1:{value}"  # 포트만 적은 경우
        elif isinstance(value, str) and value.strip():
            engines[name] = value.strip().rstrip("/")
        else:
            log.warning("config.yaml: ignoring engine %s with unusable address %r", name, value)
    return engines
