from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

COMPOSE_TIMEOUT_SECONDS: Final = 900
WAIT_TIMEOUT_SECONDS: Final = "300"
GATEWAY_SERVICE: Final = "litellm"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class ComposeGateway:
    project: str
    workdir: Path
    release: str
    port: int
    files: list[Path] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    def up(self, build: bool) -> None:
        arguments = ["up", "-d", "--wait", "--wait-timeout", WAIT_TIMEOUT_SECONDS]
        if build:
            arguments.append("--build")
        self._compose(arguments)

    def restart(self) -> None:
        self._compose(["restart", GATEWAY_SERVICE])
        self.up(False)

    def down(self) -> None:
        self._compose(["down", "-v", "--remove-orphans"])

    def logs(self) -> str:
        return self._compose(["logs", "--no-color", GATEWAY_SERVICE]).stdout

    def _compose(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command: Final = [
            "docker",
            "compose",
            "-p",
            self.project,
            "--project-directory",
            str(self.workdir),
        ]
        for compose_file in self.files:
            command.extend(["-f", str(compose_file)])
        completed: Final = subprocess.run(
            [*command, *arguments],
            capture_output=True,
            text=True,
            env=self._environment(),
            timeout=COMPOSE_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"docker compose {' '.join(arguments)} failed for {self.project}:\n"
                f"{completed.stderr[-4000:]}"
            )
        return completed

    def _environment(self) -> dict[str, str]:
        environment: Final = {
            name: value
            for name, value in os.environ.items()
            if name
            in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR"}
        }
        environment["LITELLM_TAG"] = self.release
        environment["GATEWAY_PORT"] = str(self.port)
        return environment
