from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from brownout.inputs import (
    DocumentError,
    load_yaml,
    mapping,
    positioned,
    read_text,
    with_includes,
)


@dataclass(frozen=True)
class FixedMark:
    line: int
    column: int


def test_a_position_counts_lines_and_columns_from_one():
    error = positioned(Path("config.yaml"), "is not valid YAML", FixedMark(2, 4))

    assert str(error) == "config.yaml is not valid YAML at line 3, column 5"


def test_a_problem_without_a_mark_names_the_path_only():
    error = positioned(Path("config.yaml"), "is not valid YAML", None)

    assert str(error) == "config.yaml is not valid YAML"


def test_a_missing_file_is_named(tmp_path):
    missing = tmp_path / "missing.yaml"

    with pytest.raises(DocumentError, match=f"^{missing} was not found$"):
        read_text(missing)


def test_a_file_that_cannot_be_read_names_the_reason(tmp_path):
    with pytest.raises(DocumentError, match=r"could not be read \(Is a directory\)$"):
        read_text(tmp_path)


def test_text_that_is_not_utf8_gives_an_offset_and_no_byte(tmp_path):
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"model_list:\n\xff\n")

    with pytest.raises(DocumentError) as raised:
        read_text(binary)

    assert str(raised.value) == f"{binary} is not UTF-8 text at byte 12"
    assert "ff" not in str(raised.value).removeprefix(str(binary))


def test_invalid_yaml_is_reported_by_position_only(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("version: [\n", encoding="utf-8")

    with pytest.raises(DocumentError) as raised:
        load_yaml(broken)

    assert str(raised.value) == f"{broken} is not valid YAML at line 2, column 1"


@pytest.mark.parametrize("document", [None, ["a", "b"], "text"])
def test_anything_but_a_mapping_is_refused(document):
    with pytest.raises(DocumentError, match="^config.yaml is not a YAML mapping$"):
        mapping(Path("config.yaml"), document)


def test_a_mapping_passes_through():
    assert mapping(Path("config.yaml"), {"model_list": []}) == {"model_list": []}


def test_includes_merge_the_way_litellm_merges_them(tmp_path):
    (tmp_path / "models.yaml").write_text(
        "model_list:\n  - model_name: included\nlitellm_settings:\n  drop_params: true\n",
        encoding="utf-8",
    )
    config = {
        "include": ["models.yaml"],
        "model_list": [{"model_name": "main"}],
        "litellm_settings": {"cache": False},
    }

    merged = with_includes(tmp_path / "config.yaml", config)

    assert merged == {
        "model_list": [{"model_name": "main"}, {"model_name": "included"}],
        "litellm_settings": {"drop_params": True},
    }


def test_a_missing_include_is_named(tmp_path):
    with pytest.raises(DocumentError, match="missing.yaml was not found"):
        with_includes(tmp_path / "config.yaml", {"include": ["missing.yaml"]})


def test_an_include_that_is_not_a_list_is_refused(tmp_path):
    with pytest.raises(DocumentError, match="include must list files"):
        with_includes(tmp_path / "config.yaml", {"include": "models.yaml"})


def test_a_config_without_includes_is_unchanged(tmp_path):
    assert with_includes(tmp_path / "config.yaml", {"model_list": []}) == {
        "model_list": []
    }


def test_an_included_file_that_is_not_a_mapping_is_refused(tmp_path):
    (tmp_path / "models.yaml").write_text("- model_name: a\n", encoding="utf-8")

    with pytest.raises(DocumentError, match="is not a YAML mapping"):
        with_includes(tmp_path / "config.yaml", {"include": ["models.yaml"]})


def test_an_included_file_that_includes_more_files_is_refused(tmp_path):
    (tmp_path / "models.yaml").write_text("include:\n  - more.yaml\n", encoding="utf-8")
    (tmp_path / "more.yaml").write_text("model_list: []\n", encoding="utf-8")

    with pytest.raises(DocumentError, match="models.yaml includes more files"):
        with_includes(tmp_path / "config.yaml", {"include": ["models.yaml"]})


def test_a_file_included_twice_is_refused(tmp_path):
    (tmp_path / "models.yaml").write_text("model_list: []\n", encoding="utf-8")

    with pytest.raises(DocumentError, match=r"includes \./models\.yaml twice"):
        with_includes(
            tmp_path / "config.yaml", {"include": ["models.yaml", "./models.yaml"]}
        )


def test_an_include_entry_that_is_not_a_path_is_refused(tmp_path):
    with pytest.raises(DocumentError, match="include must list files"):
        with_includes(tmp_path / "config.yaml", {"include": [3]})
