"""config.yaml loading is fail-soft and saving round-trips."""

from __future__ import annotations

from arkagent.core.config import Config, ProviderConfig, load_config, save_config
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


def test_home_env_var_selects_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ARKAGENT_HOME", str(tmp_path))

    assert ArkAgentPaths.from_env().root == tmp_path.resolve()
    assert ArkAgentPaths.from_env(tmp_path / "explicit").root == (tmp_path / "explicit").resolve()
