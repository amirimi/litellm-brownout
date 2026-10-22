from __future__ import annotations

import re
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx
import yaml

from brownout.rules import NOTICE_RULES

from .findings import PrintedFinding, parse_findings
from .gateway import ComposeGateway, free_port
from .readme import README, adapt, readme_blocks
from .resolutions import (
    MissingChoice,
    fill,
    kind_of,
    merge_config,
    primary_resolution,
)
from .scenario import Scenario
from .seed import Seeded, seed_gateway
from .shell import ShellResult, ShellSession
from .yamlfile import StyledDocument, parse_snippet, read_styled

README_GATEWAY: Final = "http://localhost:4000"
README_UVX: Final = "uvx --from litellm-brownout "
README_GATEWAY_KEY: Final = "{gateway key}"
README_RESTART: Final = "docker compose up -d"
INIT_COMMAND: Final = "brownout init"
CHECK_PASSED: Final = "brownout check passed"
LOADED: Final = "brownout loaded"
OVERRIDE_WRITTEN: Final = re.compile(
    r"^wrote (?P<path>\S+), which adds the plugin to (?P<service>\S+)$", re.MULTILINE
)
SERVED_HEADER: Final = "x-brownout-route-served"
DEGRADED_HEADER: Final = "x-brownout-degraded-from"
REFUSAL_REASON: Final = re.compile(r'"reason": *"(?P<reason>[a-z_]+)"')
TRACKED_VARIABLES: Final = ("LITELLM_MASTER_KEY", "BROWNOUT_INIT_KEY")
GATEWAY_CONFIG: Final = "config.yaml"
WRITTEN_MANIFEST: Final = "brownout.yaml"
BASE_COMPOSE_NAME: Final = "docker-compose.yml"
SCENARIO_ONLY_FILES: Final = frozenset({"scenario.yaml", "compose.yml"})
OFFLINE_TOOLS: Final = {"UV_OFFLINE": "1"}
RESTART_INSTRUCTION: Final = "restart the gateway"
PRINTED_KEY: Final = re.compile(r'"key"\s*:\s*"(sk-[^"]+)"')
PRINTED_KEY_ALIAS: Final = "printed-key"
MAX_INIT_ROUNDS: Final = 4
DRIVE_LIMIT: Final = 120
DRIVE_MAX_TOKENS: Final = 40
REQUEST_TIMEOUT_SECONDS: Final = 60.0
BUDGET_REFUSALS: Final = frozenset({422, 429})
EXCERPT: Final = 1200
NOTICES: Final = frozenset(str(rule) for rule in NOTICE_RULES)


class PathStop(Exception):
    def __init__(self, gap: str, explanation: str) -> None:
        super().__init__(f"{gap}: {explanation}")
        self.gap = gap
        self.explanation = explanation


@dataclass
class Report:
    gaps: list[str] = field(default_factory=list)
    explanations: list[str] = field(default_factory=list)
    completed: bool = False

    def record(self, gap: str, explanation: str) -> None:
        if gap not in self.gaps:
            self.gaps.append(gap)
            self.explanations.append(f"{gap}: {explanation}")

    def summary(self) -> str:
        if self.completed:
            return "\n".join(["the path completed", *self.explanations])
        return "\n".join(["the path stopped", *self.explanations])


class Adoption:
    def __init__(
        self, scenario: Scenario, release: str, wheel: Path, workdir: Path
    ) -> None:
        self.scenario: Final = scenario
        self.release: Final = release
        self.workdir: Final = workdir
        self.tool_wheel: Final = wheel
        self.report: Final = Report()
        self.blocks: Final = readme_blocks(README.read_text(encoding="utf-8"))
        self.master_key: Final = f"sk-{secrets.token_hex(16)}"
        self.gateway: Final = ComposeGateway(
            project=f"adoption-{scenario.name}-{release.replace('.', '')}",
            workdir=workdir,
            release=release,
            port=free_port(),
        )
        self.shell: Final = ShellSession(workdir, OFFLINE_TOOLS, TRACKED_VARIABLES)
        self.seeded = Seeded()
        self.init_line = ""
        self.override = Path()

    def run(self) -> Report:
        try:
            self._start()
            self._prepare()
            self._write_and_check()
            self._install()
            self._confirm()
            self._add_fallback()
            self._drive()
            self.report.completed = True
        except PathStop as stop:
            self.report.record(stop.gap, stop.explanation)
        return self.report

    def close(self) -> None:
        try:
            (self.workdir / "gateway.log").write_text(self.gateway.logs(), "utf-8")
        finally:
            self.gateway.down()

    def _start(self) -> None:
        for source in self.scenario.directory.iterdir():
            if source.name not in SCENARIO_ONLY_FILES:
                shutil.copy(source, self.workdir / source.name)
        base, *extras = self.scenario.compose_files
        shutil.copy(base, self.workdir / BASE_COMPOSE_NAME)
        self.gateway.files.append(self.workdir / BASE_COMPOSE_NAME)
        for compose_file in extras:
            self._add_compose_file(compose_file)
        (self.workdir / ".env").write_text(
            f"LITELLM_TAG={self.release}\n"
            f"GATEWAY_PORT={self.gateway.port}\n"
            f"LITELLM_MASTER_KEY={self.master_key}\n"
            f"POSTGRES_PASSWORD={secrets.token_hex(12)}\n"
            f"REDIS_PASSWORD={secrets.token_hex(12)}\n"
            "OPENAI_API_KEY=sk-mock\n"
            + "".join(
                f"{name}={value}\n" for name, value in self.scenario.environment.items()
            ),
            encoding="utf-8",
        )
        self.gateway.up(False)
        self.seeded = seed_gateway(
            self.gateway.url, self.master_key, self.scenario.seed
        )
        for name in self.scenario.templates:
            template = self.workdir / name
            template.write_text(
                self.seeded.resolve(template.read_text("utf-8")), "utf-8"
            )

    def _prepare(self) -> None:
        documented: Final = adapt(
            self._readme_block("prepare"), [(README_GATEWAY_KEY, self.master_key)]
        )
        self.init_line = _with_scenario_arguments(
            _line_with(documented, INIT_COMMAND), self.scenario.init_arguments
        )
        block: Final = documented.replace(
            _line_with(documented, INIT_COMMAND), self.init_line
        )
        result = self.shell.run(
            f"LITELLM_MASTER_KEY={self.master_key}\n"
            "typeset +x LITELLM_MASTER_KEY\n"
            f"{block}"
        )
        applied: dict[tuple[str, str], PrintedFinding] = {}
        restarted = False
        for _ in range(MAX_INIT_ROUNDS):
            findings = self._findings(result)
            if findings is None:
                return
            persisted = [finding for finding in findings if finding.key in applied]
            if persisted and not restarted:
                result = self._restart_for(persisted)
                restarted = True
                continue
            if persisted:
                raise PathStop(
                    f"{persisted[0].rule}:not-resolved",
                    f"{_named(persisted)} stayed after its printed resolution was applied "
                    "and the gateway restarted",
                )
            pending = [
                finding
                for finding in findings
                if kind_of(finding) != "none"
                and not finding.truncated
                and finding.rule not in NOTICES
            ]
            if not pending:
                raise PathStop(
                    "init:no-printed-resolution",
                    f"init exits 1, and none of these findings prints a resolution: "
                    f"{_named(findings)}",
                )
            for finding in pending:
                self._apply(finding)
                applied[finding.key] = finding
            if any(RESTART_INSTRUCTION in finding.detail for finding in pending):
                self.gateway.restart()
            result = self.shell.run(self.init_line)
        raise PathStop(
            "init:no-convergence", f"init still exits 1 after {MAX_INIT_ROUNDS} rounds"
        )

    def _findings(self, result: ShellResult) -> tuple[PrintedFinding, ...] | None:
        match result.returncode:
            case 0:
                return None
            case 1:
                return parse_findings(result.stdout)
            case code:
                raise PathStop(
                    "init:failed",
                    f"init exited {code}: {_excerpt(result.stderr + result.stdout)}",
                )

    def _restart_for(self, persisted: list[PrintedFinding]) -> ShellResult:
        self.gateway.restart()
        result: Final = self.shell.run(self.init_line)
        remaining: Final = {finding.key for finding in parse_findings(result.stdout)}
        for finding in persisted:
            if finding.key not in remaining:
                self.report.record(
                    f"{finding.rule}:needs-restart",
                    f"{finding.subject} resolved only after a gateway restart, "
                    "which neither the README nor the finding mentions",
                )
        return result

    def _apply(self, finding: PrintedFinding) -> None:
        values: Final = self.scenario.values_for(
            finding.rule, finding.subject, self.seeded.resolve
        )
        text: Final = self._filled(
            "\n".join(primary_resolution(finding)), values, finding
        )
        match kind_of(finding):
            case "command":
                self._apply_command(finding, text)
            case "config":
                self._apply_config(finding, text)
            case "none":
                raise AssertionError(f"{finding.key} has no resolution to apply")

    def _filled(
        self, text: str, values: dict[str, str], finding: PrintedFinding
    ) -> str:
        while True:
            try:
                return fill(text, values)
            except MissingChoice as missing:
                provision = self.scenario.provisions.get(missing.placeholder)
                if provision is None:
                    raise RuntimeError(
                        f"scenario {self.scenario.name} declares no value for "
                        f"{missing.placeholder} in {finding.key}"
                    ) from missing
                self._provision(provision.readme)
                values[missing.placeholder] = provision.value

    def _apply_command(self, finding: PrintedFinding, command: str) -> None:
        result: Final = self.shell.run(command)
        if result.returncode != 0 or '"error"' in result.stdout:
            raise PathStop(
                f"{finding.rule}:command-failed",
                f"the printed command for {finding.subject} failed: "
                f"{_excerpt(result.stdout + result.stderr)}",
            )
        for key in PRINTED_KEY.findall(result.stdout):
            self.seeded.keys[PRINTED_KEY_ALIAS] = key

    def _apply_config(self, finding: PrintedFinding, text: str) -> None:
        documents: Final = self._config_documents(GATEWAY_CONFIG)
        snippet: Final = parse_snippet(text)
        if not isinstance(snippet, list):
            merge_config(documents[0].data, snippet)
            documents[0].save()
            return
        unmatched = {item["model_name"] for item in snippet}
        for document in documents:
            unmatched &= set(merge_config(document.data, snippet))
        if unmatched:
            raise PathStop(
                f"{finding.rule}:nowhere-to-add",
                f"{GATEWAY_CONFIG} and its includes define no deployment named "
                f"{', '.join(sorted(unmatched))}, so the printed lines have nowhere to go",
            )
        for document in documents:
            document.save()

    def _config_documents(self, name: str) -> list[StyledDocument]:
        main: Final = read_styled(self.workdir / name)
        included: Final = [
            read_styled(self.workdir / include)
            for include in main.data.get("include") or []
        ]
        return [main, *included]

    def _write_and_check(self) -> None:
        line: Final = _with_scenario_arguments(
            _line_with(self._readme_block("write"), INIT_COMMAND),
            self.scenario.init_arguments,
        )
        written: Final = self.shell.run(line)
        if written.returncode != 0:
            raise PathStop(
                "write:failed",
                f"init --write exited {written.returncode}: "
                f"{_excerpt(written.stderr + written.stdout)}",
            )
        if CHECK_PASSED not in written.stdout:
            raise PathStop(
                "write:check-failed",
                f"init --write printed no passing check: {_excerpt(written.stdout)}",
            )
        override: Final = OVERRIDE_WRITTEN.search(written.stdout)
        if override is None:
            raise PathStop(
                "write:no-override",
                f"init --write wrote no Compose override: {_excerpt(written.stdout)}",
            )
        self.override = Path(override["path"])

    def _install(self) -> None:
        if README_RESTART not in self.blocks["restart"]:
            raise PathStop(
                "readme:block-changed",
                f"the restart block no longer runs {README_RESTART}",
            )
        self.gateway.files.append(self.override)
        self._gateway_step("install", lambda: self.gateway.up(False))
        if LOADED not in self.gateway.logs():
            raise PathStop(
                "install:not-loaded",
                f"the gateway never logged {LOADED!r}: {_excerpt(self.gateway.logs())}",
            )

    def _confirm(self) -> None:
        call: Final = self.scenario.confirm
        block: Final = adapt(
            self._readme_block("confirm"),
            [("{team key}", self.seeded.keys[call.key]), ("{model}", call.model)],
        )
        result: Final = self.shell.run(block)
        refused: Final = REFUSAL_REASON.search(result.stdout)
        if refused is not None:
            raise PathStop(
                f"confirm:{refused['reason']}",
                f"the README's confirm request was refused: {_excerpt(result.stdout)}",
            )
        if SERVED_HEADER not in result.stdout.lower():
            raise PathStop(
                "confirm:no-header",
                f"the README's confirm request printed no {SERVED_HEADER}: "
                f"{_excerpt(result.stdout + result.stderr)}",
            )

    def _add_fallback(self) -> None:
        plan: Final = self.scenario.fallback
        route_keys: Final = _manifest_template(self.blocks["fallback-manifest"])
        team_id: Final = self.seeded.resolve(plan.team)
        manifest_document: Final = read_styled(self.workdir / WRITTEN_MANIFEST)
        manifest: Final = manifest_document.data
        if team_id not in (manifest.get("teams") or {}):
            raise PathStop(
                "fallback:team-not-in-manifest",
                f"{WRITTEN_MANIFEST} has no entry for team {plan.team}",
            )
        manifest["teams"][team_id]["routes"] = {
            **(manifest["teams"][team_id].get("routes") or {}),
            plan.route: dict(
                zip(route_keys, (plan.fallback, plan.degrade_at), strict=True)
            ),
        }
        manifest_document.save()
        checked: Final = self.shell.run(self._readme_block("fallback-check"))
        if CHECK_PASSED not in checked.stdout:
            raise PathStop(
                "fallback:check-failed",
                f"brownout check failed after the fallback edits: "
                f"{_excerpt(checked.stdout + checked.stderr)}",
            )
        self._gateway_step("fallback", self.gateway.restart)

    def _drive(self) -> None:
        call: Final = self.scenario.drive
        with httpx.Client(
            base_url=self.gateway.url,
            headers={"Authorization": f"Bearer {self.seeded.keys[call.key]}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as client:
            for number in range(DRIVE_LIMIT):
                response = client.post(
                    "/v1/chat/completions", json=_drive_body(call.model, number)
                )
                if response.status_code in BUDGET_REFUSALS:
                    raise PathStop(
                        "drive:refused-before-downgrade",
                        f"LiteLLM refused with {response.status_code} before any "
                        f"downgrade: {_excerpt(response.text)}",
                    )
                if response.status_code != 200:
                    raise PathStop(
                        "drive:request-failed",
                        f"{response.status_code}: {_excerpt(response.text)}",
                    )
                if DEGRADED_HEADER in response.headers:
                    served = response.headers.get(SERVED_HEADER)
                    if served != self.scenario.fallback.fallback:
                        raise PathStop(
                            "drive:wrong-fallback",
                            f"degraded to {served}, not {self.scenario.fallback.fallback}",
                        )
                    return
        raise PathStop("drive:never-degraded", f"{DRIVE_LIMIT} requests never degraded")

    def _gateway_step(self, step: str, action: Callable[[], None]) -> None:
        try:
            action()
        except RuntimeError as failure:
            raise PathStop(
                f"{step}:gateway-did-not-start",
                f"the gateway with brownout did not become healthy: "
                f"{_excerpt(self.gateway.logs())}",
            ) from failure

    def _readme_block(self, name: str) -> str:
        return adapt(
            self.blocks[name],
            [
                (README_GATEWAY, self.gateway.url),
                *_uvx_replacement(self.blocks[name], self.tool_wheel),
            ],
        )

    def _provision(self, block: str) -> None:
        target: Final = (
            self.workdir / f"compose.{len(self.gateway.files)}.readme-{block}.yml"
        )
        target.write_text(self.blocks[block], encoding="utf-8")
        self.gateway.files.append(target)
        self.gateway.up(False)

    def _add_compose_file(self, source: Path) -> None:
        target: Final = (
            self.workdir / f"compose.{len(self.gateway.files)}.{source.name}"
        )
        shutil.copy(source, target)
        self.gateway.files.append(target)


def _drive_body(model: str, number: int) -> dict[str, object]:
    return {
        "model": model,
        "max_tokens": DRIVE_MAX_TOKENS,
        "messages": [{"role": "user", "content": f"Summarise week {number}."}],
    }


def _uvx_replacement(block: str, wheel: Path) -> list[tuple[str, str]]:
    if README_UVX not in block:
        return []
    return [(README_UVX, f"uvx --from {wheel} ")]


def _with_scenario_arguments(line: str, arguments: str) -> str:
    if not arguments:
        return line
    return f"{line} {arguments}"


def _line_with(block: str, command: str) -> str:
    lines: Final = [line for line in block.splitlines() if command in line]
    if len(lines) != 1:
        raise PathStop(
            "readme:block-changed",
            f"expected one line running {command!r}, found {len(lines)}",
        )
    return lines[0]


def _manifest_template(block: str) -> tuple[str, ...]:
    template: Final = yaml.safe_load(block)
    team: Final = next(iter(template["teams"].values()))
    route: Final = next(iter(team["routes"].values()))
    keys: Final = tuple(route)
    if keys != ("fallback", "degrade_at"):
        raise PathStop(
            "readme:block-changed", f"the fallback example's route keys are {keys}"
        )
    return keys


def _named(findings: list[PrintedFinding] | tuple[PrintedFinding, ...]) -> str:
    return "; ".join(
        f"{finding.rule} {finding.subject} ({finding.detail})" for finding in findings
    )


def _excerpt(text: str) -> str:
    return text.strip()[-EXCERPT:]


def scenario_report(
    scenario: Scenario, release: str, wheel: Path, workdir: Path
) -> Report:
    adoption: Final = Adoption(scenario, release, wheel, workdir)
    try:
        return adoption.run()
    finally:
        adoption.close()
