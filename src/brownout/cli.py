from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import httpx
from litellm.proxy._types import hash_token

from . import settings
from .bridge import configured_groups, running_litellm
from .gateway_api import (
    GatewayReadError,
    GatewaySnapshot,
    read_gateway,
    read_model_groups,
)
from .gateway_config import GatewayConfig, read_gateway_config
from .inputs import (
    DocumentError,
    load_yaml,
    load_yaml_text,
    mapping,
    read_text,
    with_includes,
)
from .manifest import Manifest, ManifestError
from .onboarding import (
    ConfigChange,
    Finding,
    SetSetting,
    admin_keys_notice,
    config_plan,
    describe_change,
    exit_code,
    key_findings,
    model_findings,
    nothing_to_govern,
    reconcile_findings,
    team_entries,
    team_findings,
    violation_findings,
)
from .onboarding_files import (
    YamlDocument,
    append_teams,
    apply_config_changes,
    dump_document,
    load_document,
    new_manifest_document,
    unified_diff,
)
from .refusals import Violation
from .rules import GatewayFile, ModelGroup, file_rules

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
    check_command.add_argument("--gateway")
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
    key: Final = settings.init_key()
    if not key:
        print(
            "init reads the gateway, so set BROWNOUT_INIT_KEY to a key that can read "
            "it, such as one for a proxy_admin_viewer user",
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
        return init(options, client, hash_token(key))


def init(options: InitOptions, client: httpx.Client, own_token: str) -> int:
    taken: Final = _taken_outputs(options)
    if taken:
        for path in taken:
            print(
                f"{path} already exists or is an input, so init will not write it",
                file=sys.stderr,
            )
        return UNREADY
    try:
        prepared = _prepare(options, client, own_token)
    except (DocumentError, GatewayReadError) as error:
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
    live_groups: Mapping[str, ModelGroup] = {}
    if arguments.gateway is not None:
        key = settings.init_key()
        if not key:
            print(
                "check --gateway reads the gateway's models, so set BROWNOUT_INIT_KEY to "
                "a key that can read it, such as one for a proxy_admin_viewer user",
                file=sys.stderr,
            )
            return UNREADY
        try:
            live_groups = _gateway_groups(arguments.gateway, key)
        except GatewayReadError as error:
            print(f"check could not read the gateway: {error}", file=sys.stderr)
            return UNREADY
    try:
        violations = check(arguments.gateway_config, arguments.manifest, live_groups)
    except DocumentError as error:
        print(error, file=sys.stderr)
        return UNREADY
    for violation in violations:
        print(f"{violation.rule}\t{violation.subject}\t{violation.message}")
    if violations:
        print(f"{len(violations)} rule violation(s)", file=sys.stderr)
        return 1
    print(f"brownout check passed on LiteLLM {running_litellm()}")
    return 0


def _gateway_groups(gateway: str, key: str) -> dict[str, ModelGroup]:
    with httpx.Client(
        base_url=gateway,
        headers={"Authorization": f"Bearer {key}"},
        timeout=GATEWAY_TIMEOUT_SECONDS,
    ) as client:
        return read_model_groups(client)


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


def _prepare(options: InitOptions, client: httpx.Client, own_token: str) -> Prepared:
    config_document: Final = load_document(
        options.gateway_config, read_text(options.gateway_config)
    )
    config: Final = read_config(
        options.gateway_config, load_yaml(options.gateway_config)
    )
    existing: Final = _existing_manifest(options.manifest)
    snapshot: Final = read_gateway(client, _key_page_progress)
    changes: Final = config_plan(config)
    apply_config_changes(config_document, changes)
    config_out_text: Final = dump_document(config_document)
    written_config: Final = read_config(
        options.gateway_config,
        load_yaml_text(options.config_out, config_out_text),
    )
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
            options, existing, snapshot, written_config, manifest_out_text, own_token
        ),
    )


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
        text = read_text(options.manifest)
        document = load_document(options.manifest, text)
        append_teams(document, entries)
        return text, document
    if not entries:
        return "", None
    return "", new_manifest_document(entries)


def _findings(
    options: InitOptions,
    existing: Manifest | None,
    snapshot: GatewaySnapshot,
    written_config: GatewayConfig,
    manifest_out_text: str | None,
    own_token: str,
) -> list[Finding]:
    findings: list[Finding] = [
        *team_findings(snapshot.teams, options.gateway),
        *key_findings(snapshot.keys, options.gateway, own_token),
        *model_findings(snapshot.groups),
    ]
    if existing is not None:
        findings.extend(reconcile_findings(existing, snapshot.teams, options.gateway))
    if manifest_out_text is None:
        findings.append(nothing_to_govern(options.gateway, bool(snapshot.teams)))
    else:
        written = Manifest.from_document(
            str(options.manifest_out),
            load_yaml_text(options.manifest_out, manifest_out_text),
        )
        findings.extend(
            violation_findings(
                file_rules(
                    written,
                    with_live_groups(gateway_file(written_config), snapshot.groups),
                )
            )
        )
    findings.append(admin_keys_notice(_own_key_teamless(snapshot, own_token)))
    return findings


def _own_key_teamless(snapshot: GatewaySnapshot, own_token: str) -> bool:
    return any(key.token == own_token and key.team_id is None for key in snapshot.keys)


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


def check(
    gateway_config: Path,
    manifest_path: Path,
    live_groups: Mapping[str, ModelGroup],
) -> list[Violation]:
    try:
        manifest = Manifest.from_file(manifest_path)
    except ManifestError as error:
        return [Violation(error.rule, str(manifest_path), str(error))]
    config: Final = read_config(gateway_config, load_yaml(gateway_config))
    return file_rules(manifest, with_live_groups(gateway_file(config), live_groups))


def read_config(path: Path, document: object) -> GatewayConfig:
    return read_gateway_config(path, with_includes(path, mapping(path, document)))


def with_live_groups(
    file: GatewayFile, live_groups: Mapping[str, ModelGroup]
) -> GatewayFile:
    return replace(file, groups={**live_groups, **file.groups})


def gateway_file(config: GatewayConfig) -> GatewayFile:
    return GatewayFile(
        groups=configured_groups(config.source, config.model_list),
        settings=config.settings,
        redis_configured=config.coordination_redis,
    )


if __name__ == "__main__":
    raise SystemExit(main())
