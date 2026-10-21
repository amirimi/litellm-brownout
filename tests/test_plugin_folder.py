from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Final

from brownout import plugin_folder

from .support import CONFIG_DIR

PACKAGE_SOURCE: Final = Path(plugin_folder.__file__).parent
LAB_MANIFEST: Final = CONFIG_DIR / "manifest.yaml"


def modules(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir() if path.suffix == ".py"}


def test_the_folder_holds_every_module_of_the_running_package(tmp_path):
    plugin_folder.write_plugin(tmp_path / "plugins")

    written = tmp_path / "plugins" / "brownout"
    assert modules(written) == modules(PACKAGE_SOURCE)
    assert not (written / "__pycache__").exists()


def test_the_folder_carries_the_version_that_wrote_it(tmp_path):
    version = plugin_folder.write_plugin(tmp_path / "plugins")

    dist_info = tmp_path / "plugins" / f"litellm_brownout-{version}.dist-info"
    assert version == plugin_folder.running_version()
    assert f"Version: {version}\n" in (dist_info / "METADATA").read_text("utf-8")


def test_python_imports_the_plugin_and_its_version_from_the_folder(tmp_path):
    version = plugin_folder.write_plugin(tmp_path / "plugins")
    probe = (
        "import importlib.metadata, brownout.policy\n"
        "print(brownout.policy.__file__)\n"
        "print(importlib.metadata.version('litellm-brownout'))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "plugins")},
    )

    imported_from, imported_version = result.stdout.splitlines()
    assert Path(imported_from).is_relative_to(tmp_path / "plugins")
    assert imported_version == version


def test_writing_again_replaces_brownout_and_leaves_other_plugins(tmp_path):
    plugins = tmp_path / "plugins"
    (plugins / "brownout").mkdir(parents=True)
    (plugins / "brownout" / "stale.py").write_text("", "utf-8")
    (plugins / "litellm_brownout-0.0.1.dist-info").mkdir()
    (plugins / "their_plugin").mkdir()

    version = plugin_folder.write_plugin(plugins)

    assert sorted(path.name for path in plugins.iterdir()) == sorted(
        ["brownout", f"litellm_brownout-{version}.dist-info", "their_plugin"]
    )
    assert not (plugins / "brownout" / "stale.py").exists()


def test_a_copy_without_package_metadata_reports_itself_unpackaged(monkeypatch):
    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(plugin_folder, "installed_version", missing)

    assert plugin_folder.running_version() == plugin_folder.UNPACKAGED


def test_the_gateway_imports_without_the_clis_dependencies():
    probe = (
        "import sys\n"
        "sys.modules['ruamel'] = None\n"
        "sys.modules['ruamel.yaml'] = None\n"
        "import brownout.gateway\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "BROWNOUT_MANIFEST": str(LAB_MANIFEST)},
    )

    assert result.returncode == 0, result.stderr[-2000:]
