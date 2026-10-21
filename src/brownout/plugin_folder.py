from __future__ import annotations

import shutil
import tempfile
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Final

PACKAGE: Final = "brownout"
DISTRIBUTION: Final = "litellm-brownout"
DIST_INFO_PREFIX: Final = "litellm_brownout-"
DIST_INFO_SUFFIX: Final = ".dist-info"
UNPACKAGED: Final = "unpackaged"
STAGING_PREFIX: Final = ".brownout-staging-"


def running_version() -> str:
    try:
        return installed_version(DISTRIBUTION)
    except PackageNotFoundError:
        return UNPACKAGED


def write_plugin(directory: Path) -> str:
    version: Final = running_version()
    directory.mkdir(parents=True, exist_ok=True)
    staging: Final = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=directory))
    try:
        _stage(staging, version)
        _swap_in(directory, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return version


def dist_info_name(version: str) -> str:
    return f"{DIST_INFO_PREFIX}{version}{DIST_INFO_SUFFIX}"


def _stage(staging: Path, version: str) -> None:
    shutil.copytree(
        Path(__file__).parent,
        staging / PACKAGE,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    dist_info: Final = staging / dist_info_name(version)
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: {version}\n",
        encoding="utf-8",
    )


def _swap_in(directory: Path, staging: Path) -> None:
    for existing in _brownout_entries(directory):
        shutil.rmtree(existing)
    for staged in staging.iterdir():
        staged.rename(directory / staged.name)


def _brownout_entries(directory: Path) -> list[Path]:
    return [
        entry
        for entry in directory.iterdir()
        if entry.name == PACKAGE
        or (
            entry.name.startswith(DIST_INFO_PREFIX)
            and entry.name.endswith(DIST_INFO_SUFFIX)
        )
    ]
