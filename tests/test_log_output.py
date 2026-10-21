from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Final

import pytest

from brownout import log_output

LITELLM_FORMAT: Final = "litellm-format %(name)s %(levelname)s %(message)s"


@pytest.fixture
def litellm_logger() -> Iterator[logging.Logger]:
    source = logging.getLogger("test-litellm-proxy")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LITELLM_FORMAT))
    source.addHandler(handler)
    yield source
    source.removeHandler(handler)


@pytest.fixture
def target() -> Iterator[logging.Logger]:
    logger = logging.getLogger("test-brownout")
    yield logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)


def test_lines_take_the_format_of_litellms_handler(litellm_logger, target, capsys):
    log_output.install(target, litellm_logger)

    target.warning('{"event": "brownout_tier_crossed"}')

    assert capsys.readouterr().out == (
        'litellm-format test-brownout WARNING {"event": "brownout_tier_crossed"}\n'
    )


def test_a_format_litellm_sets_after_loading_still_applies(
    litellm_logger, target, capsys
):
    log_output.install(target, litellm_logger)
    litellm_logger.handlers[0].setFormatter(logging.Formatter("later %(message)s"))

    target.warning("tier")

    assert capsys.readouterr().out == "later tier\n"


def test_a_warning_goes_to_stdout_and_an_error_to_stderr(
    litellm_logger, target, capsys
):
    log_output.install(target, litellm_logger)

    target.warning("team")
    target.error("incident")

    printed = capsys.readouterr()
    assert printed.out.endswith("WARNING team\n")
    assert printed.err.endswith("ERROR incident\n")
    assert "incident" not in printed.out


def test_info_lines_print_whatever_litellms_handler_level_is(
    litellm_logger, target, capsys
):
    litellm_logger.handlers[0].setLevel(logging.ERROR)
    log_output.install(target, litellm_logger)

    target.info("brownout loaded: teams=a")
    target.warning("team")

    assert capsys.readouterr().out.splitlines() == [
        "litellm-format test-brownout INFO brownout loaded: teams=a",
        "litellm-format test-brownout WARNING team",
    ]


def test_loading_twice_prints_each_line_once(litellm_logger, target, capsys):
    log_output.install(target, litellm_logger)
    log_output.install(target, litellm_logger)

    target.warning("once")

    assert capsys.readouterr().out.count("once") == 1


def test_a_handler_the_operator_attached_survives_loading(litellm_logger, target):
    own = logging.NullHandler()
    target.addHandler(own)

    log_output.install(target, litellm_logger)

    assert own in target.handlers


def test_records_still_reach_handlers_above_the_logger(
    litellm_logger, target, caplog, capsys
):
    log_output.install(target, litellm_logger)

    target.warning("exported")

    assert [record.getMessage() for record in caplog.records] == ["exported"]


def test_a_litellm_logger_without_a_handler_stops_loading(target):
    with pytest.raises(log_output.LiteLLMLogHandlerMissing):
        log_output.install(target, logging.getLogger("test-litellm-without-handler"))
