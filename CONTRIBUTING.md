# Contributing

A change passes three suites before it merges. The unit suite runs from a clean clone with no
services. The seam suite asserts the LiteLLM behaviours the plugin relies on. The end-to-end
sequence drives the composed gateway from `docker-compose.yml` over HTTP. CI runs `ruff format
--check`, `ruff check`, `mypy`, the unit and seam suites and `brownout check` on the lab config in
one job, the end-to-end sequence in a second, and the same sequence against a gateway image with the
built wheel installed through the documented line in a third.

## Running the tests

```bash
cp .env.example .env
uv sync
uv run pytest -m "not e2e"
```

The unit and seam suites hold 315 tests and need no Redis or gateway. One boundary seam skips on
each release, because the shrink and refuse-whole seams split by release. `decide` and `resolve` are
pure functions tested with plain values. Every outside dependency of the pipeline sits behind a
protocol in `src/brownout/ports.py`, so the gateway tests inject fakes, and the permission and
guardrail tests drive LiteLLM's real helpers. `tests/test_rules.py` and `tests/test_cli.py` run the
shared rule set against the lab config and against one fixture per rule.

`tests/test_litellm_seams.py` imports none of the plugin. Each test names one LiteLLM behaviour the
plugin depends on, such as the signature of a private helper, the shape of a reservation entry or
guardrails merging before the pre-call hook. A LiteLLM release that changes one of them fails with
that behaviour's name.

The end-to-end sequence holds 10 tests and runs on mock routes. It spends no provider money and
still exercises real reservations, a real Redis counter and the native ceiling. It reads the
plugin's announcements from `docker compose logs litellm` and the request facts from LiteLLM's
`/spend/logs`. Real-provider
quality and concurrent admission across gateway replicas stay outside every suite.

```bash
docker compose up -d --wait
set -a && source .env && set +a
uv run pytest -m e2e
```

The sequence skips itself when the gateway is unreachable. `LAB_REQUIRE_STACK=1` turns an
unreachable gateway into a failure, which is what CI does. The sequence provisions the one lab team
the manifest names, so two copies cannot run at once.

`tests/setup_lab.py` is rerunnable at about eleven seconds, because a spend reset takes more than one
write. LiteLLM buffers spend per team and flushes it every `PROXY_BATCH_WRITE_AT` seconds. A counter
cleared in Redis is reseeded from the database row on the next read. The script therefore deletes
the team, waits out the flush window, clears the counter and the claims, then recreates the team.

## Supported LiteLLM releases

Brownout supports LiteLLM 1.95.1 and newer. `SUPPORTED_FLOOR`, `REFUSE_WHOLE_FROM` and
`TESTED_CEILING` in `src/brownout/bridge.py` hold the range and the budget boundary. Below the floor
the handler refuses to start. On 1.95.1 to 1.100 it warns that LiteLLM shrinks an over-budget
reservation instead of refusing it, and past the ceiling it warns that the release is untested. The
floor moves only by a decision recorded in the pull request that moves it. Releases before 1.95
differ in the guardrail merge and in the helpers the bridge calls, 1.95.0 carries a LiteLLM security
advisory, and releases before 1.84 place no reservation at all.

`pyproject.toml` pins the floor, so the ci workflow runs the unit and seam suites on it. The
`litellm matrix` workflow runs every suite against the latest patch of each supported release line,
on every pull request, weekly and on demand. The boundary seam and the end-to-end counter bound both
follow the release under test. To run one release line by hand, install it into the project's
environment and point the lab at the matching image.

```bash
uv pip install --no-config "litellm[proxy]==1.104.2"
uv run --no-sync pytest -m "not e2e"
LITELLM_TAG=v1.104.2 docker compose up -d --wait
LAB_REQUIRE_STACK=1 uv run --no-sync pytest -m e2e
```

A new release joins the range when all three suites pass on it. Add the release to the workflow's
matrix and raise `TESTED_CEILING`. The end-to-end sequence accepts LiteLLM's budget refusal as a 429
or a 422, because 1.104 changed the default code to 422. A seam failure names the LiteLLM behaviour
that changed, and the fix belongs in `src/brownout/bridge.py`, the only module that touches
LiteLLM's internals.

## Releasing

The `release` workflow builds the sdist and the wheel on a `v*` tag, stops when the tag differs from
`pyproject.toml`'s version, and publishes to PyPI through trusted publishing. Its publish job is the
only job in the repository with `id-token: write`, and no PyPI token is stored anywhere.

1. Once, before the first release, register a pending trusted publisher on pypi.org for
   `amk9978/litellm-brownout`, workflow `release.yml`, environment `pypi`, and create the `pypi`
   environment in the repository settings.
2. Set the version in `pyproject.toml`, update the CHANGELOG, and merge.
3. Tag the merge commit as `v` plus the version and push the tag.

Running the workflow by hand on a branch builds the artifacts without publishing them.
