# LiteLLM Brownout

**Switch selected routes to cheaper models before the team budget runs out.**

A team has $500 for the week. At $450, routine Q&A can move to a smaller model. Code review keeps its primary model. Both still count toward the same budget.

Brownout adds that policy to an existing LiteLLM gateway. You choose the fallback and the percentage that triggers it. Clients keep requesting the same model name; response headers tell them when it changed. LiteLLM continues to handle reservations, spend accounting, and budget admission.

[![CI](https://github.com/amk9978/litellm-brownout/actions/workflows/ci.yml/badge.svg)](https://github.com/amk9978/litellm-brownout/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Tested with **LiteLLM 1.101.0**. The demo uses mock model responses and requires no provider credentials.

## See it run

Start the gateway, create a $1 lab budget, then send enough requests to cross its thresholds. You will see the served route change, followed by LiteLLM's budget rejection when the remaining headroom cannot cover another reservation.

Requires Docker Compose, Python 3.12+, `uv`, and `curl`. Run from Bash.

```bash
git clone https://github.com/amk9978/litellm-brownout.git
cd litellm-brownout
cp .env.example .env
uv sync --locked
docker compose up -d --wait

set -a
source .env
set +a

KEY="$(uv run python lab/setup_lab.py)"

for i in $(seq 1 70); do
  curl -sS -D - -o /dev/null \
    http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"budget-lab-primary","messages":[{"role":"user","content":"hi"}],"max_tokens":40}' \
    | grep -iE '^HTTP/|^x-platform-'
done
```

The lab switches at 75%, emits another warning at 90%, and eventually returns HTTP 429. A degraded response includes:

```http
HTTP/1.1 200 OK
x-platform-route-served: budget-lab-degraded
x-platform-budget-tier: 75
x-platform-degraded-from: budget-lab-primary
x-platform-degradation-reason: degrade_tier_reached
```

Read the events:

```bash
curl -fsS http://localhost:8088/events | python3 -m json.tool
```

LiteLLM, Redis, and PostgreSQL are real processes. Model responses and token prices are synthetic, so this demonstrates routing and accounting without provider charges. The supplied credentials are for local development.

To repeat the demo, rerun `lab/setup_lab.py` and the request loop. The setup script deletes and recreates the `budget-lab` team and clears its counters and event claims; keep it pointed at the demo gateway. Stop the stack with `docker compose down`.

## Choose what can degrade

Warning thresholds belong to the team. Each route decides whether, and when, it can switch models.

For the $500 example:

| Committed team spend | Q&A route | Code review route | Signal |
|---|---|---|---|
| Below $375 | Primary | Primary | No tier event |
| $375–$449.99 | Primary | Primary | 75% warning |
| $450 and above | Declared fallback, if permitted | Primary | 90% warning and downgrade disclosure |
| Insufficient headroom for the requested route's reservation | Native budget rejection | Native budget rejection | HTTP 429 |

The corresponding `manifest.yaml`:

```yaml
version: "v1"

teams:
  team-a:
    policy_epoch: "team-a-v1"
    envelope_usd: 500.0
    budget_duration: "7d"
    warn_tiers: [75, 90]
    on_counter_unavailable: block
    routes:
      chat-qa:
        fallback: chat-qa-degraded
        degrade_at: 90
      code-review: {}
```

All the team's routes contribute to the total. An empty route policy—or a route absent from the manifest—keeps its primary model. A route's `degrade_at` also becomes an event threshold, even if it is absent from `warn_tiers`.

Change `policy_epoch` when deploying a revised policy to re-arm its event claims. Configuration loads at process startup.

The threshold uses LiteLLM's spend counter, including other requests' outstanding reservations, minus the current request's own reservation. It can therefore run ahead of settled spend. Decisions are evaluated per request; degradation is not latched until reset.

## Add it to your gateway

Define your model pair and matching catalog entries, mount the plugin, enable its callback, then create the team and its application key.

Use **LiteLLM 1.101.0**. Brownout imports private LiteLLM helpers, so compatibility with other versions is not assumed. Mounting into the stock image is tested; installing the wheel into a custom image is not.

1. **Configure actual provider models.** Use the route names from your manifest. Set `input_cost_per_token` and `output_cost_per_token` on each deployment, and keep them equal to the matching entry in [prices.yaml](config/prices.yaml). Set the matching `model_info.data_class`. The supplied [gateway config](config/config.yaml) uses `mock_response`; replace those mock definitions when connecting providers.

2. **Mount the package and configuration.** Add these settings to your existing Compose service:

   ```yaml
   services:
     litellm:
       image: docker.litellm.ai/berriai/litellm:v1.101.0
       environment:
         PYTHONPATH: /app/plugins
         BUDGET_POLICY_MANIFEST: /app/config/manifest.yaml
         BUDGET_POLICY_PRICES: /app/config/prices.yaml
       volumes:
         - ./src/budget_policy:/app/plugins/budget_policy:ro
         - ./config:/app/config:ro
       command: ["--config", "/app/config/config.yaml", "--port", "4000"]
   ```

3. **Enable the callback and native reservations.** Merge these into the gateway configuration:

   ```yaml
   general_settings:
     fail_closed_budget_enforcement: true
     coordination_redis:
       host: os.environ/REDIS_HOST
       port: 6379
       password: os.environ/REDIS_PASSWORD

   litellm_settings:
     callbacks: ["budget_policy.adapter.handler"]
   ```

   Keep budget reservation enabled. Use deployment failover within a model group; cross-group router fallbacks can make Brownout's disclosure inaccurate. The supplied configuration leaves native budget alerting off to avoid duplicate notifications.

4. **Create the team and key.** The native team's budget amount and duration must match the manifest. Both the team and key must be allowed to call the fallback. For the example above:

   ```bash
   curl -fsS http://localhost:4000/team/new \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"team_id":"team-a","team_alias":"team-a","max_budget":500.0,"budget_duration":"7d","models":["chat-qa","chat-qa-degraded","code-review"]}'

   curl -fsS http://localhost:4000/key/generate \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"team_id":"team-a","key_alias":"team-a-app","max_budget":null,"models":[]}'
   ```

   Use the returned application key for chat requests. An additional key budget creates another admission limit; the example uses only the team budget.

The callback applies to chat-completion calls across the gateway. A key with no team, or a team absent from the manifest, receives 503. Enabling it on a shared gateway therefore requires manifest entries for every team whose chat traffic it will handle.

## What callers and operators see

On successful governed responses:

| Header | Meaning |
|---|---|
| `x-platform-route-served` | Selected model group |
| `x-platform-deployment-id` | Deployment ID, when LiteLLM supplies it |
| `x-platform-budget-tier` | Highest reached threshold |
| `x-platform-degraded-from` | Original route, when rewritten |
| `x-platform-degradation-reason` | `degrade_tier_reached` or `fallback_rejected`, when applicable |

If the caller cannot access the fallback, Brownout keeps the primary and reports `fallback_rejected`. A separate rejection event ensures this remains visible even if another route already claimed the team's tier event.

| Event | Trigger |
|---|---|
| `budget_tier_crossed` | A declared percentage is reached |
| `budget_fallback_rejected` | A due downgrade fails its entitlement check |
| `budget_policy_incident` | Enforcement cannot proceed normally, such as an unreadable counter or budget mismatch |

Events are structured logs. Set `BUDGET_POLICY_WEBHOOK_URL` to receive the same JSON by HTTP, or `DD_API_KEY` to enable Datadog metrics (`DD_SITE` and `DD_ENV` configure the destination and environment).

Redis deduplicates tier events per team, policy epoch, window, and threshold. Rejection events additionally distinguish the route and reason. Remote delivery is best effort; a failed delivery is not retried for that claim. Events describe applied routing decisions, not completed provider responses.

## Before using real traffic

Start with a text-chat workload for which you have checked the fallback's quality and request compatibility.

| Boundary | Current behavior |
|---|---|
| Fallback validation | Startup checks require priced, distinct routes and a target data-class label at least as restrictive as the primary's. These labels do not verify provider region, retention, or compliance. |
| Request compatibility | Context capacity, tools, structured output, and modalities are not checked. With `drop_params: true`, unsupported parameters may be dropped. |
| Fallback cost | Relative prices are not checked. Choose a cheaper pair for brownout use. A dearer fallback can exceed the primary reservation and overshoot the budget. |
| Budget admission | LiteLLM reserves against the requested primary before Brownout runs. A request rejected there cannot be rescued by switching to a cheaper model. Reservation accuracy remains a LiteLLM dependency. |
| Reset windows | The shipped configuration uses `7d` and UTC. Other durations need care: `2d` produces a moving claim window, `2mo` passes validation but raises during window calculation, and DST can destabilize reset boundaries. Counter resets are asynchronous. |
| Configuration validation | Unknown manifest keys are currently ignored. A typo such as `degrade_At` silently leaves degradation disabled. |
| Spend-read failure | `on_counter_unavailable` defaults to `block` (503); `admit` and `degrade` are available. Native fail-closed authentication may reject the request before this setting applies. |
| Access and coverage | Entitlement uses direct key/team model lists; access groups and aliases can cause valid targets to be rejected. Other call types pass through untouched. Platform headers are supplied on the success path only. |

The plugin checks the runtime team's budget amount on each request and rejects a mismatch. It does not check the runtime duration. Keep that aligned when provisioning teams.

## Why add this to LiteLLM?

LiteLLM's [native budget fallbacks](https://docs.litellm.ai/docs/proxy/budget_fallbacks) switch models when a virtual key exhausts a per-model budget. Brownout makes the decision earlier, at a percentage of a **shared team budget**, and lets each route choose its response.

Use it when premium quality is worth paying for while budget remains, but a cheaper response is still useful near the limit. Keep routes that cannot tolerate the switch on their primary models.

## Tests and contributions

```bash
uv run pytest -m "not e2e"
uv run ruff format --check .
uv run ruff check .
uv run mypy

# With the demo stack running and .env exported:
LAB_REQUIRE_STACK=1 uv run pytest -m e2e
```

CI runs 123 unit/configuration tests and two serial gateway tests. The gateway tests exercise real reservations and Redis counters using mock models. Real-provider quality and concurrent admission through multiple gateway replicas are not established by that suite.

The decision lives in [policy.py](src/budget_policy/policy.py); LiteLLM integration lives in [adapter.py](src/budget_policy/adapter.py). [Configuration tests](tests/test_config_contract.py) check the supplied manifest, catalog, and gateway config together. Adapt those checks when changing your own deployment.

Trying this on a real workload? [Open an issue](https://github.com/amk9978/litellm-brownout/issues) with the model pair, budget window, threshold, and whether the fallback remained useful. Reports of failed switches and unacceptable outputs are particularly helpful. Leave keys and private prompts out.

## License

[MIT](LICENSE).
