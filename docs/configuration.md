# Configuration

A governed gateway needs four pieces in place before its first request. The plugin package is
mounted into the stock `docker.litellm.ai/berriai/litellm:v1.101.0` image, the gateway config names
the handler, the manifest and the price catalog describe each team and model, and each team exists
in LiteLLM with a `max_budget` equal to its manifest envelope. The handler validates the manifest
and the catalog when it loads, and then checks every request against them.

## Mount the package

```yaml
services:
  litellm:
    image: docker.litellm.ai/berriai/litellm:v1.101.0
    environment:
      PYTHONPATH: /app/plugins
      BUDGET_POLICY_PRICES: /app/config/prices.yaml
      BUDGET_POLICY_MANIFEST: /app/config/manifest.yaml
    volumes:
      - ./src/budget_policy:/app/plugins/budget_policy:ro
      - ./config:/app/config:ro
    command: ["--config", "/app/config/config.yaml", "--port", "4000"]
```

The plugin imports `httpx`, `pyyaml`, `fastapi` and `litellm`, all present in the image. Three of
its LiteLLM imports are private helpers (`_can_object_call_model`,
`_resolve_key_models_for_auth_check` and `_check_and_merge_model_level_guardrails`), so the version
pin is load-bearing. `pyproject.toml` pins `litellm[proxy]==1.101.0`. The repository documents and
tests the mount. Installing the built wheel into a custom image is untested.

| Variable | Default | Purpose |
|---|---|---|
| `BUDGET_POLICY_MANIFEST` | `config/manifest.yaml` | Team budgets, tiers and route policies |
| `BUDGET_POLICY_PRICES` | `config/prices.yaml` | Committed per-token prices and data classes |
| `BUDGET_POLICY_WEBHOOK_URL` | unset | Every tier and incident event is also POSTed here as JSON |
| `DD_API_KEY` | unset | Enables the Datadog metric sink |
| `DD_SITE` | `datadoghq.com` | Datadog site for the series API |
| `DD_ENV` | `unknown` | Value of the `env` tag on every series |

## Gateway config

```yaml
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  fail_closed_budget_enforcement: true
  coordination_redis:
    host: os.environ/REDIS_HOST
    port: 6379
    password: os.environ/REDIS_PASSWORD

litellm_settings:
  callbacks: ["budget_policy.adapter.handler"]

model_list:
  - model_name: route-chat
    litellm_params:
      model: provider-x/large-model
      api_key: os.environ/PROVIDER_X_API_KEY
      input_cost_per_token: 0.000003
      output_cost_per_token: 0.000015
    model_info:
      data_class: customer_pii

  - model_name: route-chat-degraded
    litellm_params:
      model: provider-x/small-model
      api_key: os.environ/PROVIDER_X_API_KEY
      input_cost_per_token: 0.00000025
      output_cost_per_token: 0.00000125
    model_info:
      data_class: customer_pii
```

`provider-x` stands for a provider prefix LiteLLM recognises. Each key does one job.

| Setting | Why it is there |
|---|---|
| `fail_closed_budget_enforcement: true` | Decides the envelope boundary for the whole gateway, read from `general_settings` alone ([`proxy/proxy_server.py:2429`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2429)). On, a worst case that does not fit the remaining headroom is refused whole at authentication ([`proxy/spend_tracking/budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141)), and so is a reservation that cannot be written or a spend figure that cannot be verified ([`budget_reservation.py:259`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L259), [`proxy_server.py:2527`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2527)). Off, the hold shrinks to the headroom and the request ships at its full `max_tokens`, so a window can close one request above its envelope. Removing the key gives that soft boundary to every team. `disable_budget_reservation` is a third posture with no hold at all, described under [the envelope boundary](enforcement.md#the-envelope-boundary) |
| `coordination_redis` | The spend counter LiteLLM increments per team. The plugin's tier claim reuses the same Redis, so one crossing is one event across every replica. Without Redis the claim falls back to a per-replica set and logs that once |
| `callbacks` | The dotted path to the handler instance `adapter.py` builds at import |
| `input_cost_per_token`, `output_cost_per_token` on each deployment | What LiteLLM reserves against. `tests/test_config_contract.py` requires them to equal the catalog entry for the same model group |
| `model_info.data_class` | Must equal the catalog's class for the group. The plugin reads the catalog, and the contract test keeps the two aligned |

Three keys must stay absent. `general_settings.alerting` would make LiteLLM's hardcoded 85% and
95% alerter a second owner of tier events. `router_settings.fallbacks`, or its context-window and
content-policy variants, would move a request to another model group after the disclosure header
was decided. `general_settings.disable_budget_reservation` would remove the hold from every budget
on the gateway. `tests/test_config_contract.py` fails on any of the three, and on a missing
`fail_closed_budget_enforcement`.

## Create the team

Every team in the manifest needs a LiteLLM team whose `max_budget` equals `envelope_usd` and whose
`budget_duration` equals the manifest's, with a model list that includes every declared fallback.
Keys carry no budget of their own, because a key budget would be a second ceiling the manifest
knows nothing about. `lab/setup_lab.py` is the reference implementation and sends these two
requests.

```bash
curl -s localhost:4000/team/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "team-a", "team_alias": "team-a", "max_budget": 500.0, "budget_duration": "7d", "models": ["route-chat", "route-chat-degraded"]}'

curl -s localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "team-a", "key_alias": "team-a-app", "max_budget": null, "models": []}'
```

The plugin checks the amount on every request. When `team.max_budget` drifts from `envelope_usd`
it refuses with a 503 and raises a platform incident. The duration is invisible at request time, so
a drifted `budget_duration` has to be caught wherever the team is rendered.

## Team manifest

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
      route-chat:
        fallback: route-chat-degraded
        degrade_at: 90
      route-batch:
        fallback: null
        degrade_at: null
```

| Key | Type | Rule |
|---|---|---|
| `version` | string | Required. Reported in incidents |
| `teams.<team_id>` | mapping | Keyed by the LiteLLM `team_id`. A key whose team is absent here is refused with a 503 and a platform incident |
| `policy_epoch` | string | Required. Part of every tier claim key, so an approved policy change re-arms every tier inside the current window |
| `envelope_usd` | positive finite number | The owner-approved budget. Must equal the LiteLLM team's `max_budget` |
| `budget_duration` | LiteLLM duration string, such as `30s`, `30m`, `24h`, `7d`, `1mo` | Parsed at load with LiteLLM's own parser. Identifies the window and sizes every claim TTL. `7d` anchors the window to Monday at the configured reset time |
| `warn_tiers` | non-empty list of integers in 1..99 | Each is a percentage of the envelope that raises one event per team per window. 100 belongs to the native ceiling and is refused at load |
| `on_counter_unavailable` | `block`, `degrade` or `admit` | The team's posture when the spend counter cannot be read. Default `block` |
| `routes.<model_group>` | mapping, may be empty | Keyed by LiteLLM model group name. A route absent from the manifest still spends against the envelope and still earns tier events. It never degrades |
| `routes.<model_group>.fallback` | model group name or `null` | The degradation target. Checked against the catalog when the handler loads, as described under [fallback validation](#fallback-validation). Its price relative to the route is the owner's call |
| `routes.<model_group>.degrade_at` | integer in 1..99 or `null` | Requires `fallback`. Joined to the team's tier set, so a downgrade raises an event even at a percentage the team did not list |
| `routes.<model_group>.on_counter_unavailable` | as above | Per-route override of the team posture |

## Price catalog

```yaml
version: "v1"

data_class_order:
  - internal
  - customer_pii

models:
  route-chat:
    input_cost_per_token: 0.000003
    output_cost_per_token: 0.000015
    data_class: customer_pii
  route-chat-degraded:
    input_cost_per_token: 0.00000025
    output_cost_per_token: 0.00000125
    data_class: customer_pii
```

`data_class_order` lists classes least sensitive first. Every model needs both costs as finite,
non-negative numbers with at least one above zero, and a `data_class` from the order. A route the
gateway serves and the catalog does not price is refused with a 503 and a platform incident,
because LiteLLM returns `None` for an unpriced model and skips reservation entirely.

## Fallback validation

A declared fallback is checked twice, once when the handler loads and once per request.

The load check covers what the manifest and the catalog fix for the life of the process. Both
routes are priced, the fallback differs from its route, and its data class sits at or above the
route's in `data_class_order`. A pairing that fails refuses the handler, so the gateway does not
start. The fallback's price relative to its route stays with the manifest's owner, who approved the
pairing. A fallback that costs more speeds spending toward the 429 while the header still says
degraded.

The request check asks LiteLLM's own `_can_object_call_model` whether both the key and the team may
call the fallback. A refusal serves the primary. The response header and the tier event carry
`fallback_rejected`, and a `budget_fallback_rejected` event names the route, the target and the
rejection. That event has its own claim per route and rejection per window, because a tier claim
belongs to the team and a route with no fallback may already hold it.

An entitlement check that raises anything other than LiteLLM's `ProxyException` is a platform
fault. The plugin serves the primary and raises an incident alongside the rejection event, so the
fault reaches the platform owner instead of reading as a narrow allowlist.
