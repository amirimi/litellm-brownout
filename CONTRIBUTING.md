# Contributing

A change passes two suites before it merges. The unit suite runs from a clean clone with no
services. The end-to-end sequence drives the composed gateway from `docker-compose.yml` over HTTP.
CI runs `ruff format --check`, `ruff check`, `mypy` and the unit suite in one job, and the
end-to-end sequence in a second.

## Running the tests

```bash
cp .env.example .env
uv sync
uv run pytest -m "not e2e"
```

The unit suite holds 131 tests and needs no Redis or gateway. The decision function is pure, and
the adapter's two impure edges, the spend counter and the tier store, are injected.
`tests/test_config_contract.py` holds the cross-file checks that fail an unpriced or unserved route
at review time, before it reaches a gateway.

The end-to-end sequence runs on mock routes. It spends no provider money and still exercises real
reservations, a real Redis counter and the native ceiling.

```bash
docker compose up -d --wait
set -a && source .env && set +a
uv run pytest -m e2e
```

The sequence skips itself when the gateway is unreachable. `LAB_REQUIRE_STACK=1` turns an
unreachable gateway into a failure, which is what CI does. The sequence provisions the one lab team
the manifest names, so two copies cannot run at once.

`lab/setup_lab.py` is rerunnable at about eleven seconds, because a spend reset takes more than one
write. LiteLLM buffers spend per team and flushes it every `PROXY_BATCH_WRITE_AT` seconds. A counter
cleared in Redis is reseeded from the database row on the next read. The script therefore deletes
the team, waits out the flush window, clears the counter and the claims, then recreates the team.

## Moving to a newer LiteLLM

The image tag sits on line 3 of `docker-compose.yml`, and `pyproject.toml` pins
`litellm[proxy]==1.101.0`. The plugin imports three private LiteLLM helpers, so a new tag is
trusted only after both suites pass against it.

```bash
docker compose pull && docker compose up -d --wait
uv run pytest -m "not e2e"
LAB_REQUIRE_STACK=1 uv run pytest -m e2e
```
