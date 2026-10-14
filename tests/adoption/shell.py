from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PRESERVED_ENVIRONMENT: Final = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "UV_CACHE_DIR",
    "SSL_CERT_FILE",
)
STATE_FILE: Final = ".shell-state"
TRANSCRIPT_FILE: Final = "transcript.log"
STEP_TIMEOUT_SECONDS: Final = 900


@dataclass(frozen=True)
class ShellResult:
    returncode: int
    stdout: str
    stderr: str


class ShellSession:
    def __init__(
        self,
        workdir: Path,
        environment: Mapping[str, str],
        tracked: Sequence[str],
    ) -> None:
        self._workdir: Final = workdir
        self._tracked: Final = tuple(tracked)
        self._environment: Final = {
            name: os.environ[name]
            for name in PRESERVED_ENVIRONMENT
            if name in os.environ
        } | dict(environment)

    def run(self, script: str, stdin: str = "") -> ShellResult:
        completed: Final = subprocess.run(
            ["bash", "-c", self._wrapped(script)],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=self._workdir,
            env=self._environment,
            timeout=STEP_TIMEOUT_SECONDS,
            check=False,
        )
        with (self._workdir / TRANSCRIPT_FILE).open(
            "a", encoding="utf-8"
        ) as transcript:
            transcript.write(
                f"$ {script.rstrip()}\n[exit {completed.returncode}]\n"
                f"{completed.stdout}{completed.stderr}\n"
            )
        return ShellResult(completed.returncode, completed.stdout, completed.stderr)

    def _wrapped(self, script: str) -> str:
        state: Final = self._workdir / STATE_FILE
        saved: Final = " ".join(self._tracked)
        save_line = ":"
        if saved:
            save_line = f"declare -p {saved} > {state} 2>/dev/null"
        return (
            f"if [ -f {state} ]; then . {state}; fi\n"
            f"{script}\n"
            "__adoption_status=$?\n"
            "umask 077\n"
            f"{save_line}\n"
            "exit $__adoption_status\n"
        )
