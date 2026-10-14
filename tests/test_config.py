"""config.yaml loading is fail-soft and saving round-trips."""

from __future__ import annotations

import re
from pathlib import Path

from arkagent.core.config import DEFAULT_ENGINES, Config, ProviderConfig, load_config, save_config
from arkagent.core.paths import ArkAgentPaths


def test_missing_file_boots_with_defaults(tmp_path):
    cfg = load_config(ArkAgentPaths.from_env(tmp_path))

    assert cfg == Config()
    assert cfg.provider.kind == "ollama"


def test_known_keys_merge_over_defaults_and_unknown_keys_are_ignored(tmp_path):
    paths = ArkAgentPaths.from_env(tmp_path).ensure()
    paths.config.write_text(
        "provider:\n  kind: openai\n  model: local-model\n  base_url: http://localhost:8080/v1\n  retired_knob: 1\n"
        "retired_section:\n  x: 1\n",
        encoding="utf-8",
    )

    cfg = load_config(paths)

    assert cfg.provider.kind == "openai"
    assert cfg.provider.model == "local-model"
    assert cfg.provider.base_url == "http://localhost:8080/v1"
    assert cfg.provider.num_ctx == ProviderConfig().num_ctx


def test_broken_yaml_boots_with_defaults(tmp_path):
    paths = ArkAgentPaths.from_env(tmp_path).ensure()

    paths.config.write_text("provider: [unclosed\n", encoding="utf-8")
    assert load_config(paths) == Config()

    paths.config.write_text("- just\n- a list\n", encoding="utf-8")
    assert load_config(paths) == Config()


def test_save_then_load_round_trips(tmp_path):
    paths = ArkAgentPaths.from_env(tmp_path / "home")
    cfg = Config(provider=ProviderConfig(kind="ollama", model="qwen", keep_alive="30m", idle_unload_seconds=9))

    save_config(paths, cfg)

    assert load_config(paths) == cfg
    assert not paths.config.with_name("config.yaml.tmp").exists()


def test_engines_default_to_the_ports_in_the_contract_document():
    contract = (Path(__file__).parent.parent / "docs" / "ENGINE-CONTRACT.md").read_text(encoding="utf-8")
    documented = {name: f"http://127.0.0.1:{port}" for name, port in re.findall(r"^\| (\w+) \| (\d+) ", contract, re.M)}

    assert documented == DEFAULT_ENGINES
    assert Config().engines == DEFAULT_ENGINES


def test_engines_section_overrides_adds_and_removes(tmp_path):
    paths = ArkAgentPaths.from_env(tmp_path).ensure()
    paths.config.write_text(
        "engines:\n"
        "  tts: http://127.0.0.1:50000/\n"  # 주소를 바꾼다
        "  video: 47840\n"  # 포트만 적어 엔진을 더한다
        "  music: null\n"  # 뺀다
        "  'bad name': 47841\n"
        "  broken: [1, 2]\n",
        encoding="utf-8",
    )

    assert load_config(paths).engines == {
        "assets": DEFAULT_ENGINES["assets"],
        "tts": "http://127.0.0.1:50000",
        "video": "http://127.0.0.1:47840",
    }


def test_home_env_var_selects_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ARKAGENT_HOME", str(tmp_path))

    assert ArkAgentPaths.from_env().root == tmp_path.resolve()
    assert ArkAgentPaths.from_env(tmp_path / "explicit").root == (tmp_path / "explicit").resolve()
