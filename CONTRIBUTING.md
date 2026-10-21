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

The unit and seam suites hold 588 tests and need no Redis or gateway. A few seams skip on each
release, because the LiteLLM behaviours they pin begin or end at a release. `decide` and `resolve` are
pure functions tested with plain values. Every outside dependency of the pipeline sits behind a
protocol in `src/brownout/ports.py`, so the gateway tests inject fakes, and the permission and
guardrail tests drive LiteLLM's real helpers. `tests/test_rules.py` and `tests/test_cli.py` run the
shared rule set against the lab config and against one fixture per rule.

`tests/test_litellm_seams.py` imports none of the plugin. Each test names one LiteLLM behaviour the
plugin depends on, such as the signature of a private helper, the shape of a reservation entry or
guardrails merging before the pre-call hook. A LiteLLM release that changes one of them fails with
that behaviour's name.

The end-to-end sequence holds 15 tests and runs on mock routes. Six more tests in
`tests/test_printed_commands_e2e.py` run the commands `init` prints against the gateway and read back
the change. It spends no provider money and
still exercises real reservations, a real Redis counter and the native ceiling. It reads the
plugin's announcements from `docker compose logs litellm` and the request facts from LiteLLM's
`/spend/logs`. Real-provider
quality and concurrent admission across gateway replicas stay outside every suite.

```bash
docker compose up -d --wait
set -a && source .env && set +a
uv run pytest -m e2e
```

`tests/test_call_kinds_e2e.py` drives a second lab team, `kinds-lab`, through each call kind. It
sends every text endpoint past the downgrade tier, then embeddings, a direct MCP tool call, an MCP
tool loop and a batch that brownout refuses. The `lab-tools` service answers the tool loop and the
embeddings and serves the MCP tool. It runs on the LiteLLM image, so no request leaves the lab.

The suites skip themselves when the gateway is unreachable. `LAB_REQUIRE_STACK=1` turns an
unreachable gateway into a failure, which is what CI does. The suites provision the `budget-lab`
and `kinds-lab` teams the manifest names, so two copies cannot run at once.

`tests/setup_lab.py` is rerunnable at about eleven seconds, because a spend reset takes more than one
write. LiteLLM buffers spend per team and flushes it every `PROXY_BATCH_WRITE_AT` seconds. A counter
cleared in Redis is reseeded from the database row on the next read. The script therefore deletes
the team, waits out the flush window, clears the counter and the claims, then recreates the team.

## Adoption paths

The adoption suite adopts brownout on stock LiteLLM gateways the way an operator would. For each
scenario in `tests/adoption/scenarios/`, the runner starts its own compose project on a free port
and seeds teams, keys and models through LiteLLM's API. It then runs the README's marked blocks as
written and applies each fix `init` prints, filling in only the placeholders the scenario declares.
The run continues through `--write`, `check`, the install line, the confirm request, a first
fallback and a drive to the downgrade. Mock responses keep every run free.

```bash
BROWNOUT_ADOPTION=1 uv run pytest -m adoption
BROWNOUT_ADOPTION=1 BROWNOUT_ADOPTION_SCENARIOS=a-config-models BROWNOUT_ADOPTION_RELEASES=v1.101.6 uv run pytest -m adoption
```

A step the runner cannot take from printed text is a gap. Each scenario lists its gaps in
`expected_gaps`, and its test passes only when a run meets exactly those. A fix that closes a gap
fails the test until its entry is removed. A new gap fails it until someone decides whether to fix
or record it. A gap that ends in `?` may or may not occur, because it depends on LiteLLM's timing,
such as a config reload that lands before or after `init` reruns. The runner substitutes three things in the README's blocks: the gateway URL, the local
wheel for the PyPI package, and the LiteLLM image tag. Every step's command and output lands in
`transcript.log` in the scenario's temporary directory.

The README marks the blocks the runner runs with `<!-- adoption: name -->` comments, so a marker
moves with its block. A scenario directory holds `scenario.yaml`, the gateway's `config.yaml`, and
optionally a `compose.yml` overlay and any file the config includes. The `adoption` workflow runs
every scenario on 1.101.6 and 1.106.1 weekly and on pull requests that touch `init`, `check`, the
README or the docs. It also runs `a-config-models` and `b-ui-models` on every supported release line,
because those two run the README's commands against LiteLLM's API.

## Input ledger

`tests/ledger/` lists every input that can change what brownout does, and `tests/test_ledger.py`
keeps the list and the code in step. A new input gets one parsing function first, then a row, then a
test for each form the row names.

- A row names the input's shape, its parsing function, the raw reads that function makes, and the
  forms the input can take. A form is supported, refused at the earliest stage that can see it,
  documented when no stage can see it, or open while it waits on a decision. `OPEN_FINDINGS` in
  `tests/ledger/inventory.py` names each open decision, so closing one removes its entry.
- `tests/ledger/scan.py` finds every key, attribute, environment variable, flag and LiteLLM call
  brownout reads. The ledger test fails on a read without a row and on a row whose read the code no
  longer makes. The scan sees keys written as literals or module constants, so a helper that takes
  the key as a parameter needs its row written by hand.
- A row about LiteLLM cites its code as `path:line@release`, at a release the ledger walked.
- Raising the `litellm[proxy]` bound in `pyproject.toml` fails the ledger test until the new minor
  line is walked. Walk LiteLLM's config loading, its request path from `add_litellm_data_to_request`
  to the router's deployment choice, and `common_checks` at the new release. Then add the release to
  `WALKED_RELEASES`, `litellm-matrix.yml` and the adoption workflow's README scenarios.

## Supported LiteLLM releases

Brownout supports LiteLLM 1.101.6 and newer. `SUPPORTED_FLOOR` and `TESTED_CEILING` in
`src/brownout/bridge.py` hold the range. Below the floor the handler refuses to start, and past the
ceiling it warns that the release is untested. The floor moves only by a decision recorded in the
pull request that moves it. Releases before 1.101 shrink an over-budget reservation to the remaining
headroom instead of refusing it, so a window could close above the envelope.

`pyproject.toml` pins the floor, so the ci workflow runs the unit and seam suites on it. The
`litellm matrix` workflow runs every suite against the latest patch of each supported release line,
on every pull request, weekly and on demand. The boundary seam and the end-to-end counter bound both
follow the release under test. To run one release line by hand, install it into the project's
environment and point the lab at the matching image.

```bash
uv pip install --no-config "litellm[proxy]==1.106.1"
uv run --no-sync pytest -m "not e2e"
LITELLM_TAG=v1.106.1 docker compose up -d --wait
LAB_REQUIRE_STACK=1 uv run --no-sync pytest -m e2e
```

A new release joins the range when it has been walked into the input ledger and all three suites
pass on it. Add the release to the workflow's matrix and raise `TESTED_CEILING`. The end-to-end sequence accepts LiteLLM's budget refusal as a 429
or a 422, because 1.104 changed the default code to 422. A seam failure names the LiteLLM behaviour
that changed, and the fix belongs in `src/brownout/bridge.py`, the only module that touches
LiteLLM's internals.

## Releasing

The `release` workflow builds the sdist and the wheel on a `v*` tag, stops when the tag differs from
`pyproject.toml`'s version, and publishes to PyPI through trusted publishing. Its publish job is the
only job in the repository with `id-token: write`, and no PyPI token is stored anywhere.

1. Once, before the first release, the owner sets up four things.
   - On pypi.org, a pending trusted publisher for `amirimi/litellm-brownout`, workflow
     `release.yml`, environment `pypi`.
   - In the repository settings, a `pypi` environment that requires the owner's review, so no
     workflow publishes without an approval.
   - A tag ruleset that lets only the owner create `v*` tags.
   - Private vulnerability reporting, which SECURITY.md names as the channel.
2. Set the version in `pyproject.toml`, update the CHANGELOG, and merge.
3. Tag the merge commit as `v` plus the version and push the tag.
4. Approve the `publish to PyPI` job in the workflow run.

Running the workflow by hand on a branch builds the artifacts without publishing them. Every
workflow pins uv, and `pyproject.toml` pins `uv_build`, to the same release. A new pin names a
release at least two weeks old, because a compromised release is usually caught and yanked within
days.
