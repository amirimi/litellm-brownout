from __future__ import annotations

import re
import tomllib
from collections import Counter
from pathlib import Path
from typing import Final, assert_never

import yaml

from brownout.refusals import RefusalReason, RuleId
from brownout.rules import NOTICE_RULES

from .ledger.inventory import LEDGER, OPEN_FINDINGS, WALKED_RELEASES
from .ledger.model import (
    FIRST_MINOR,
    LAST_MINOR,
    Disposition,
    Documented,
    Form,
    Open,
    Refused,
    Supported,
)
from .ledger.scan import package_functions, qualified_functions, scan_package
from .support import REPO_ROOT

PACKAGE: Final = REPO_ROOT / "src" / "brownout"
MATRIX_WORKFLOW: Final = REPO_ROOT / ".github" / "workflows" / "litellm-matrix.yml"
ADOPTION_WORKFLOW: Final = REPO_ROOT / ".github" / "workflows" / "adoption.yml"
README_SCENARIOS: Final = ("a-config-models", "b-ui-models")
PYPROJECT: Final = REPO_ROOT / "pyproject.toml"
TEST_ID: Final = re.compile(
    r"^(?P<path>tests/[\w/]+\.py)::(?P<name>[\w:]+?)(?:\[.*\])?$"
)
CITATION: Final = re.compile(r"^litellm/[\w/]+\.py:\d+@(?P<release>1\.\d+\.\d+)$")
LITELLM_REQUIREMENT: Final = re.compile(
    r"^litellm\[proxy\]>=1\.\d+\.\d+,<1\.(?P<minor>\d+)$"
)
CITING_SOURCES: Final = frozenset({"litellm", "printed"})


def test_every_read_in_brownout_belongs_to_exactly_one_input() -> None:
    claims: Final = _claims()
    unclaimed: Final = [
        f"{read.site} {read.label}"
        for read in scan_package(PACKAGE)
        if (read.site, read.label) not in claims
    ]
    shared: Final = [
        f"{site} {label}: {', '.join(shapes)}"
        for (site, label), shapes in claims.items()
        if len(shapes) > 1
    ]
    assert not unclaimed, "reads with no ledger row:\n" + "\n".join(unclaimed)
    assert not shared, "reads claimed by two rows:\n" + "\n".join(shared)


def test_every_row_names_reads_its_parser_makes() -> None:
    scanned: Final = {(read.site, read.label) for read in scan_package(PACKAGE)}
    stale: Final = [
        f"{row.shape}: {row.parser} {label}"
        for row in LEDGER
        if row.parser is not None
        for label in row.reads
        if (row.parser, label) not in scanned
    ]
    assert not stale, "rows naming reads the code no longer makes:\n" + "\n".join(stale)


def test_every_shape_has_one_row() -> None:
    repeated: Final = [
        shape
        for shape, count in Counter(row.shape for row in LEDGER).items()
        if count > 1
    ]
    assert not repeated, f"shapes with more than one row: {repeated}"


def test_every_parser_is_a_brownout_function() -> None:
    functions: Final = package_functions(PACKAGE)
    missing: Final = [
        f"{row.shape}: {row.parser}"
        for row in LEDGER
        if row.parser is not None and row.parser not in functions
    ]
    assert not missing, f"parsers that do not exist: {missing}"


def test_every_supported_or_refused_form_names_an_existing_test() -> None:
    problems: list[str] = []
    for row in LEDGER:
        for form in row.forms:
            problems.extend(
                f"{row.shape}, {form.form}: {problem}"
                for problem in _test_problems(form)
            )
    assert not problems, "\n".join(problems)


def test_every_rule_refuses_some_form() -> None:
    rules: Final = {refusal.rule for refusal in _refusals() if refusal.rule is not None}
    assert set(RuleId) - NOTICE_RULES - rules == set()


def test_a_notice_rule_refuses_nothing() -> None:
    rules: Final = {refusal.rule for refusal in _refusals() if refusal.rule is not None}
    assert NOTICE_RULES & rules == set()


def test_every_refusal_reason_refuses_some_form() -> None:
    reasons: Final = {refusal.reason for refusal in _refusals()}
    assert set(RefusalReason) - reasons == set()


def test_open_forms_are_the_known_findings() -> None:
    findings: Final = {
        form.disposition.finding
        for row in LEDGER
        for form in row.forms
        if isinstance(form.disposition, Open)
    }
    assert findings == set(OPEN_FINDINGS)


def test_every_documented_form_is_in_its_docs() -> None:
    missing: Final = [
        f"{row.shape}, {form.form}: {form.disposition.docs}"
        for row in LEDGER
        for form in row.forms
        if isinstance(form.disposition, Documented)
        and form.disposition.phrase not in _text(REPO_ROOT / form.disposition.docs)
    ]
    assert not missing, f"documented forms the docs do not state: {missing}"


def test_litellm_rows_cite_walked_litellm_code() -> None:
    problems: Final = [
        f"{row.shape}: {cite!r}"
        for row in LEDGER
        for cite in row.cites
        if not _cites_walked_release(cite)
    ]
    assert not problems, f"citations outside the walked releases: {problems}"


def test_every_litellm_row_cites_code() -> None:
    uncited: Final = [
        row.shape for row in LEDGER if row.source in CITING_SOURCES and not row.cites
    ]
    assert not uncited, f"rows with no LiteLLM citation: {uncited}"


def test_the_walked_releases_are_the_ci_matrix() -> None:
    workflow: Final = yaml.safe_load(MATRIX_WORKFLOW.read_text(encoding="utf-8"))
    matrix: Final = workflow["jobs"]["release-line"]["strategy"]["matrix"]["litellm"]
    assert tuple(matrix) == WALKED_RELEASES


def test_the_readme_commands_run_on_every_walked_release() -> None:
    workflow: Final = yaml.safe_load(ADOPTION_WORKFLOW.read_text(encoding="utf-8"))
    matrix: Final = workflow["jobs"]["scenario"]["strategy"]["matrix"]
    runs: Final = {
        (scenario, release)
        for scenario in matrix["scenario"]
        for release in matrix["release"]
    } | {(entry["scenario"], entry["release"]) for entry in matrix["include"]}
    expected: Final = {
        (scenario, f"v{release}")
        for scenario in README_SCENARIOS
        for release in WALKED_RELEASES
    }
    assert expected - runs == set()


def test_raising_the_litellm_bound_needs_a_new_walk() -> None:
    walked_minors: Final = [_minor(release) for release in WALKED_RELEASES]
    assert walked_minors == list(range(FIRST_MINOR, LAST_MINOR + 1))
    assert _upper_bound_minor() == LAST_MINOR + 1


def _claims() -> dict[tuple[str, str], list[str]]:
    claims: dict[tuple[str, str], list[str]] = {}
    for row in LEDGER:
        if row.parser is None:
            continue
        for label in row.reads:
            claims.setdefault((row.parser, label), []).append(row.shape)
    return claims


def _refusals() -> list[Refused]:
    return [
        form.disposition
        for row in LEDGER
        for form in row.forms
        if isinstance(form.disposition, Refused)
    ]


def _test_problems(form: Form) -> list[str]:
    tests: Final = _tests_of(form.disposition)
    if tests is None:
        return []
    if not tests:
        return ["names no test"]
    return [problem for test in tests for problem in _missing_test(test)]


def _tests_of(disposition: Disposition) -> tuple[str, ...] | None:
    match disposition:
        case Supported(tests=tests):
            return tests
        case Refused(tests=tests):
            return tests
        case Documented() | Open():
            return None
        case _:
            assert_never(disposition)


def _missing_test(test: str) -> list[str]:
    match_: Final = TEST_ID.match(test)
    if match_ is None:
        return [f"{test!r} is not a test id"]
    path: Final = REPO_ROOT / match_["path"]
    if not path.is_file():
        return [f"{match_['path']} does not exist"]
    if match_["name"].replace("::", ".") not in qualified_functions(path):
        return [f"{test} does not exist"]
    return []


def _cites_walked_release(cite: str) -> bool:
    match_: Final = CITATION.match(cite)
    if match_ is None:
        return False
    return match_["release"] in WALKED_RELEASES


def _minor(release: str) -> int:
    return int(release.split(".")[1])


def _upper_bound_minor() -> int:
    project: Final = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    requirements: Final = [
        LITELLM_REQUIREMENT.match(requirement)
        for requirement in project["dependencies"]
    ]
    bounds: Final = [
        int(match_["minor"]) for match_ in requirements if match_ is not None
    ]
    assert len(bounds) == 1, "pyproject.toml must pin litellm[proxy] to one range"
    return bounds[0]


def _text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")
