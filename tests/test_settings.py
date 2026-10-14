from __future__ import annotations

from brownout import settings


def test_the_manifest_path_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("BROWNOUT_MANIFEST", "/etc/brownout/manifest.yaml")

    assert settings.manifest_path() == "/etc/brownout/manifest.yaml"


def test_the_manifest_path_defaults_to_the_config_directory(monkeypatch):
    monkeypatch.delenv("BROWNOUT_MANIFEST", raising=False)

    assert settings.manifest_path() == "config/manifest.yaml"


def test_the_init_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("BROWNOUT_INIT_KEY", "sk-viewer")

    assert settings.init_key() == "sk-viewer"


def test_no_init_key_is_set_by_default(monkeypatch):
    monkeypatch.delenv("BROWNOUT_INIT_KEY", raising=False)

    assert settings.init_key() is None
