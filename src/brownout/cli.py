from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final

import httpx
import litellm
import yaml
from litellm.types.integrations.slack_alerting import DEFAULT_ALERT_TYPES

from . import settings
from .gateway_api import GatewayReadError, GatewaySnapshot, read_gateway
from .manifest import Manifest, ManifestError
from .onboarding import (
    ConfigChange,
    Finding,
    SetSetting,
    config_plan,
    describe_change,
    exit_code,
    key_findings,
    master_key_notice,
    model_findings,
    nothing_to_govern,
    reconcile_findings,
    team_entries,
    team_findings,
    violation_findings,
)
from .onboarding_files import (
    DocumentError,
    YamlDocument,
    append_teams,
    apply_config_changes,
    dump_document,
    load_document,
    new_manifest_document,
    to_plain,
    unified_diff,
)
from .refusals import Violation
from .rules import (
    GatewayFile,
    GatewaySettings,
    ModelGroup,
    file_rules,
)

GATEWAY_TIMEOUT_SECONDS: Final = 30.0
NEW_FILE_MODE: Final = 0o666
PERMISSION_BITS: Final = 0o777
FINDINGS_PER_RULE: Final = 20
UNREADY: Final = 2


@dataclass(frozen=True)
class InitOptions:
    gateway: str
    gateway_config: Path
    manifest: Path | None
    manifest_out: Path
    config_out: Path
    write: bool


@dataclass(frozen=True)
class Prepared:
    config_out_text: str
    changes: tuple[ConfigChange, ...]
    manifest_text: str
    manifest_out_text: str | None
    findings: list[Finding]


def main(argv: Sequence[str] | None = None) -> int:
    arguments: Final = _parser().parse_args(argv)
    if arguments.command == "init":
        return _init_command(arguments)
    return _check_command(arguments)


def _parser() -> argparse.ArgumentParser:
    parser: Final = argparse.ArgumentParser(prog="brownout")
    commands: Final = parser.add_subparsers(dest="command", required=True)
    check_command: Final = commands.add_parser(
        "check", help="Check a gateway config and a manifest against brownout's rules"
    )
    check_command.add_argument("--gateway-config", required=True, type=Path)
    check_command.add_argument("--manifest", required=True, type=Path)
    init_command: Final = commands.add_parser(
        "init",
        help="Read a running gateway and prepare a manifest and a gateway config",
    )
    init_command.add_argument("--gateway", required=True)
    init_command.add_argument("--gateway-config", required=True, type=Path)
    init_command.add_argument("--manifest", type=Path)
    init_command.add_argument(
        "--manifest-out", type=Path, default=Path("brownout.yaml")
    )
    init_command.add_argument(
        "--config-out", type=Path, default=Path("brownout.config.yaml")
    )
    init_command.add_argument("--write", action="store_true")
    return parser


def _init_command(arguments: argparse.Namespace) -> int:
    key: Final = settings.master_key()
    if not key:
        print(
            "init reads the gateway as an admin, so set LITELLM_MASTER_KEY",
            file=sys.stderr,
        )
        return UNREADY
    options: Final = InitOptions(
        gateway=arguments.gateway,
        gateway_config=arguments.gateway_config,
        manifest=arguments.manifest,
        manifest_out=arguments.manifest_out,
        config_out=arguments.config_out,
        write=arguments.write,
    )
    with httpx.Client(
        base_url=options.gateway,
        headers={"Authorization": f"Bearer {key}"},
        timeout=GATEWAY_TIMEOUT_SECONDS,
    ) as client:
        return init(
            options, client, [alert_type.value for alert_type in DEFAULT_ALERT_TYPES]
        )


def init(
    options: InitOptions, client: httpx.Client, default_alert_types: Sequence[str]
) -> int:
    taken: Final = _taken_outputs(options)
    if taken:
        for path in taken:
            print(
                f"{path} already exists or is an input, so init will not write it",
                file=sys.stderr,
            )
        return UNREADY
    try:
        prepared = _prepare(options, client, default_alert_types)
    except (OSError, DocumentError, GatewayReadError) as error:
        print(
            f"init could not read its inputs or the gateway: {error}", file=sys.stderr
        )
        return UNREADY
    except ManifestError as error:
        print(f"{error.rule}\t{options.manifest}\t{error}", file=sys.stderr)
        return UNREADY
    _report(options, prepared)
    if options.write:
        _write(options, prepared)
    return exit_code(prepared.findings)


def _check_command(arguments: argparse.Namespace) -> int:
    violations: Final = check(arguments.gateway_config, arguments.manifest)
    for violation in violations:
        print(f"{violation.rule}\t{violation.subject}\t{violation.message}")
    if violations:
        print(f"{len(violations)} rule violation(s)", file=sys.stderr)
        return 1
    print(f"brownout check passed on LiteLLM {litellm_release()}")
    return 0


def _taken_outputs(options: InitOptions) -> list[Path]:
    if not options.write:
        return []
    inputs: Final = {options.gateway_config.resolve()}
    if options.manifest is not None:
        inputs.add(options.manifest.resolve())
    return [
        path
        for path in (options.manifest_out, options.config_out)
        if path.exists() or path.resolve() in inputs
    ]


def _prepare(
    options: InitOptions, client: httpx.Client, default_alert_types: Sequence[str]
) -> Prepared:
    config_document: Final = _load_document(
        options.gateway_config, options.gateway_config.read_text(encoding="utf-8")
    )
    config: Final = to_plain(config_document)
    if not isinstance(config, dict):
        raise DocumentError(f"{options.gateway_config} is not a YAML mapping")
    existing: Final = _existing_manifest(options.manifest)
    snapshot: Final = read_gateway(client, _key_page_progress)
    changes: Final = config_plan(config, default_alert_types)
    apply_config_changes(config_document, changes)
    config_out_text: Final = dump_document(config_document)
    manifest_text, manifest_document = _manifest_documents(options, existing, snapshot)
    manifest_out_text: str | None = None
    if manifest_document is not None:
        manifest_out_text = dump_document(manifest_document)
    return Prepared(
        config_out_text=config_out_text,
        changes=changes,
        manifest_text=manifest_text,
        manifest_out_text=manifest_out_text,
        findings=_findings(
            options, existing, snapshot, config_document, manifest_document
        ),
    )


def _load_document(path: Path, text: str) -> YamlDocument:
    try:
        return load_document(text)
    except DocumentError as error:
        raise DocumentError(f"{path} {error}") from error


def _existing_manifest(path: Path | None) -> Manifest | None:
    if path is None:
        return None
    return Manifest.from_file(path)


def _manifest_documents(
    options: InitOptions, existing: Manifest | None, snapshot: GatewaySnapshot
) -> tuple[str, YamlDocument | None]:
    known: Final = frozenset(existing.team_ids if existing is not None else ())
    entries: Final = team_entries(snapshot.teams, known)
    if options.manifest is not None:
        text = options.manifest.read_text(encoding="utf-8")
        document = _load_document(options.manifest, text)
        append_teams(document, entries)
        return text, document
    if not entries:
        return "", None
    return "", new_manifest_document(entries)


def _findings(
    options: InitOptions,
    existing: Manifest | None,
    snapshot: GatewaySnapshot,
    config_document: YamlDocument,
    manifest_document: YamlDocument | None,
) -> list[Finding]:
    findings: list[Finding] = [
        *team_findings(snapshot.teams, options.gateway),
        *key_findings(snapshot.keys, options.gateway),
        *model_findings(snapshot.models),
    ]
    if existing is not None:
        findings.extend(reconcile_findings(existing, snapshot.teams, options.gateway))
    if manifest_document is None:
        findings.append(nothing_to_govern())
    else:
        written = Manifest.from_document(
            str(options.manifest_out), to_plain(manifest_document)
        )
        findings.extend(
            violation_findings(
                file_rules(written, gateway_file(to_plain(config_document)))
            )
        )
    findings.append(master_key_notice())
    return findings


def _key_page_progress(page: int, total: int) -> None:
    if total > 1:
        print(f"reading keys, page {page} of {total}", file=sys.stderr)


def _report(options: InitOptions, prepared: Prepared) -> None:
    _print_changes(options, prepared.changes)
    for change in prepared.changes:
        if isinstance(change, SetSetting) and change.note:
            print(f"note: {change.section}.{change.key} {change.note}")
    print(
        "note: settings saved through LiteLLM's admin UI live in its database and are "
        "not part of these changes"
    )
    if prepared.manifest_out_text is not None:
        _print_diff(
            prepared.manifest_text, prepared.manifest_out_text, options.manifest_out
        )
    _print_findings(prepared.findings)
    if not options.write:
        print("dry run: nothing written. Rerun with --write to write both files")
    blocking: Final = sum(1 for finding in prepared.findings if finding.blocking)
    if blocking:
        print(
            f"{blocking} finding(s) to resolve before enabling brownout",
            file=sys.stderr,
        )
    else:
        print(
            "ready: enable the callback once brownout check passes on the written files",
            file=sys.stderr,
        )


def _print_changes(options: InitOptions, changes: Sequence[ConfigChange]) -> None:
    if not changes:
        print(f"{options.config_out}: no changes")
        return
    print(f"{options.config_out} changes {options.gateway_config} as follows:")
    for change in changes:
        print(f"  {describe_change(change)}")


def _print_diff(before: str, after: str, path: Path) -> None:
    diff: Final = unified_diff(before, after, str(path))
    if diff:
        print(diff, end="")
    else:
        print(f"{path}: no changes")


def _print_findings(findings: Sequence[Finding]) -> None:
    by_rule: dict[str, list[Finding]] = {}
    for finding in findings:
        by_rule.setdefault(str(finding.rule), []).append(finding)
    for rule, group in by_rule.items():
        for finding in group[:FINDINGS_PER_RULE]:
            print(f"{rule}\t{finding.subject}\t{finding.detail}")
            for line in finding.resolution:
                print(f"    {line}")
        if len(group) > FINDINGS_PER_RULE:
            print(f"{rule}\t...\tand {len(group) - FINDINGS_PER_RULE} more")


def _write(options: InitOptions, prepared: Prepared) -> None:
    source_mode: Final = options.gateway_config.stat().st_mode & PERMISSION_BITS
    _write_new(options.config_out, prepared.config_out_text, source_mode)
    print(f"wrote {options.config_out}")
    if prepared.manifest_out_text is not None:
        _write_new(options.manifest_out, prepared.manifest_out_text, NEW_FILE_MODE)
        print(f"wrote {options.manifest_out}")


def _write_new(path: Path, text: str, mode: int) -> None:
    descriptor: Final = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with open(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def check(gateway_config: Path, manifest_path: Path) -> list[Violation]:
    try:
        manifest = Manifest.from_file(manifest_path)
    except ManifestError as error:
        return [Violation(error.rule, str(manifest_path), str(error))]
    return file_rules(manifest, gateway_file(_load_yaml(gateway_config)))


def gateway_file(config: Mapping[str, Any]) -> GatewayFile:
    general: Final = config.get("general_settings") or {}
    deployments_by_group: dict[str, list[Mapping[str, Any]]] = {}
    for deployment in config.get("model_list") or []:
        deployments_by_group.setdefault(str(deployment["model_name"]), []).append(
            deployment
        )
    return GatewayFile(
        groups={
            name: _model_group(name, deployments)
            for name, deployments in deployments_by_group.items()
        },
        settings=GatewaySettings(
            fail_closed_budget_enforcement=general.get("fail_closed_budget_enforcement")
            is True,
            disable_budget_reservation=general.get("disable_budget_reservation")
            is True,
        ),
        native_budget_alerts=_native_budget_alerts(general),
        redis_configured=bool(general.get("coordination_redis")),
    )


def _native_budget_alerts(general: Mapping[str, Any]) -> bool:
    if not general.get("alerting"):
        return False
    alert_types = general.get("alert_types")
    if alert_types is None:
        return True
    return "budget_alerts" in alert_types


def litellm_release() -> str:
    return version("litellm")


def _model_group(name: str, deployments: list[Mapping[str, Any]]) -> ModelGroup:
    prices = [_deployment_prices(deployment) for deployment in deployments]
    input_costs = [price[0] for price in prices]
    output_costs = [price[1] for price in prices]
    return ModelGroup(
        name=name,
        input_cost_per_token=_highest_known(input_costs),
        output_cost_per_token=_highest_known(output_costs),
        data_classes=tuple(
            (deployment.get("model_info") or {}).get("data_class")
            for deployment in deployments
        ),
    )


def _deployment_prices(
    deployment: Mapping[str, Any],
) -> tuple[float | None, float | None]:
    params: Final = deployment.get("litellm_params") or {}
    mapped: Final = _cost_map_entry(str(params.get("model") or ""))
    return (
        _price(params.get("input_cost_per_token"), mapped.get("input_cost_per_token")),
        _price(
            params.get("output_cost_per_token"), mapped.get("output_cost_per_token")
        ),
    )


def _cost_map_entry(model: str) -> Mapping[str, Any]:
    exact = litellm.model_cost.get(model)
    if exact is not None:
        return exact
    return litellm.model_cost.get(model.split("/", 1)[-1]) or {}


def _price(explicit: Any, mapped: Any) -> float | None:
    value = explicit
    if value is None:
        value = mapped
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _highest_known(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    if len(known) != len(values) or not known:
        return None
    return max(known)


def _load_yaml(path: Path) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        return {}
    return document


if __name__ == "__main__":
    raise SystemExit(main())
