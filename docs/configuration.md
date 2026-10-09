# Configuration

A governed gateway needs four pieces in place before its first request. The `brownout` package is
mounted into the stock LiteLLM image, the gateway config names the handler, the manifest describes
each team, and each team exists in LiteLLM with a `max_budget` equal to its manifest envelope.
`brownout check` runs the file rules over the gateway config and the manifest in CI. The handler
validates the manifest when it loads and runs the request rules on every governed request.

## Mount the package

```yaml
services:
  litellm:
    image: docker.litellm.ai/berriai/litellm:v1.104.2
    environment:
      PYTHONPATH: /app/plugins
      BROWNOUT_MANIFEST: /app/config/manifest.yaml
    volumes:
      - ./src/brownout:/app/plugins/brownout:ro
      - ./config:/app/config:ro
    command: ["--config", "/app/config/config.yaml", "--port", "4000"]
```

The plugin imports `httpx`, `pyyaml`, `fastapi` and `litellm`, all present in the image. Three of
its LiteLLM imports are private helpers (`_can_object_call_model`,
`_resolve_key_models_for_auth_check` and `_check_and_merge_model_level_guardrails`), and all of
them live in `src/brownout/bridge.py`. The handler refuses to start on LiteLLM below 1.101.0 and
logs one warning on a release newer than the tested 1.104. The repository documents and tests the
mount. Installing the built wheel into a custom image is untested.

| Variable | Default | Purpose |
|---|---|---|
| `BROWNOUT_MANIFEST` | `config/manifest.yaml` | Team budgets, tiers and route policies |
| `BROWNOUT_WEBHOOK_URL` | unset | Every event is also POSTed here as JSON |
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
  callbacks: ["brownout.gateway.handler"]

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
| `fail_closed_budget_enforcement: true` | Decides the envelope boundary for the whole gateway, read from `general_settings` alone ([`proxy/proxy_server.py:2429`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2429)). On, a worst case that does not fit the remaining headroom is refused whole at authentication ([`proxy/spend_tracking/budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141)), and so is a reservation that cannot be written or a spend figure that cannot be verified. Rule `fail-closed-on` requires it |
| `coordination_redis` | The spend counter LiteLLM increments per team. The plugin's claims reuse the same Redis, so one crossing is one event across every replica. Rule `redis-configured` requires it in `brownout check` |
| `callbacks` | The dotted path to the handler `src/brownout/gateway.py` builds at import |
| `input_cost_per_token`, `output_cost_per_token` | What LiteLLM reserves against. A deployment without them is priced from LiteLLM's cost map. Rules `route-priced` and `model-priced` refuse a model with neither |
| `model_info.data_class` | Read for a route that declares a fallback and for the fallback. Every deployment of the group declares the same class, from the manifest's `data_class_order`. Rules `data-class-declared` and `fallback-data-class` |

Three keys must stay absent. `general_settings.alerting` would make LiteLLM's hardcoded 85% and 95%
alerter a second owner of tier events, which rule `no-native-alerting` refuses.
`router_settings.fallbacks`, or its context-window and content-policy variants, would move a
request to another model group after the disclosure header was decided, which rule
`no-router-fallbacks` refuses. `general_settings.disable_budget_reservation` would remove the hold
from every budget on the gateway, which rule `reservation-on` refuses.

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

The plugin checks the amount and the key budget on every request, and refuses with a 503 and a
platform incident when either breaks its assumption. The duration is invisible at request time, so
a drifted `budget_duration` has to be caught wherever the team is rendered.

## Team manifest

```yaml
version: "v1"

data_class_order:
  - internal
  - customer_pii

teams:
  team-a:
    policy_epoch: "team-a-v1"
    envelope_usd: 500.0
    budget_duration: "7d"
    warn_tiers: [75, 90]
    routes:
      route-chat:
        fallback: route-chat-degraded
        degrade_at: 90
        warn_tiers: [60, 80]
      route-batch: {}
```

| Key | Type | Rule |
|---|---|---|
| `version` | string | Required. Reported in the handler's load log |
| `data_class_order` | list of distinct strings, least restricted first | Required when any route declares a fallback |
| `teams.<team_id>` | mapping | Keyed by the LiteLLM `team_id`. A key whose team is absent here is refused with a 503 and a platform incident |
| `policy_epoch` | string | Required. Part of every claim key, so an approved policy change re-arms every transition inside the current window |
| `envelope_usd` | positive finite number | The owner-approved budget. Must equal the LiteLLM team's `max_budget` |
| `budget_duration` | `1h`, `2h`, `3h`, `4h`, `6h`, `8h`, `12h`, `24h`, `1d`, `7d`, `1w`, `30d`, `1mo`, `hourly`, `daily`, `weekly` or `monthly` | The windows that tumble with LiteLLM's resets. Any other value fails rule `duration-tumbles` |
| `warn_tiers` | list of integers in 1..99, optional | Team tiers. Each raises one event with scope `team` per window. 100 belongs to LiteLLM's ceiling |
| `routes.<model_group>` | mapping, may be empty | Keyed by LiteLLM model group name. A route absent from the manifest still spends against the envelope and still earns team tier events. It never degrades |
| `routes.<model_group>.fallback` | model group name | The degradation target. It differs from its route (rule `fallback-distinct`), is served and priced, and is at least as restricted in data class. Its price relative to the route is the owner's call |
| `routes.<model_group>.degrade_at` | integer in 1..99 | Requires `fallback`. A degrade threshold only, and never a tier |
| `routes.<model_group>.warn_tiers` | list of integers in 1..99, optional | Route tiers. Each raises one event with scope `route` per window, on a request to that route |

An unknown key at any level fails rule `manifest-schema` and names the key, so a typo such as
`degrade_At` stops the manifest from loading.

## The rules

Each rule has an id. `brownout check` runs the file rules, the handler runs the manifest rules when
it loads, and the request rules run on every governed request against LiteLLM's live Router and
settings. A request rule reads only what that request touches, which is its route, its fallback and
the gateway settings.

| Id | Checks | Runs in |
|---|---|---|
| `manifest-schema` | Required keys, types, tier ranges and strict keys | Check, load |
| `duration-tumbles` | `budget_duration` is one of the accepted windows | Check, load |
| `fallback-distinct` | A fallback differs from its route | Check, load |
| `route-served` | Every manifest route and fallback is a gateway model group | Check, request |
| `route-priced` | Every manifest route and fallback has input and output prices | Check, request |
| `data-class-declared` | A fallback pairing's deployments each declare one class from `data_class_order` | Check, request |
| `fallback-data-class` | A fallback is at least as restricted as its route | Check, request |
| `fail-closed-on` | `fail_closed_budget_enforcement` is `true` | Check, request |
| `reservation-on` | `disable_budget_reservation` is absent or `false` | Check, request |
| `no-router-fallbacks` | No `fallbacks`, `context_window_fallbacks` or `content_policy_fallbacks` | Check, request |
| `no-native-alerting` | `general_settings.alerting` is absent | Check |
| `redis-configured` | `coordination_redis` is set | Check |
| `no-caller-routing` | The caller sent no routing fields or reserved metadata | Request |
| `team-in-manifest` | The key's team is in the manifest | Request |
| `key-has-no-budget` | The key carries no `max_budget`, `model_max_budget` or `budget_limits` of its own | Request |
| `model-priced` | The requested model is priced | Request |
| `ceiling-matches-envelope` | The team's `max_budget` equals `envelope_usd` | Request |
| `spend-readable` | The team's spend counter can be read | Request |
| `supported-litellm` | LiteLLM is 1.101.0 or newer | Load |

```bash
uv run brownout check --gateway-config config/config.yaml --manifest config/manifest.yaml
```

The command prints one line per violation, as rule id, subject and message, and exits non-zero
when any rule fails. It reads `config.yaml` as YAML and fills a deployment's missing prices from
LiteLLM's cost map, so it runs without starting a gateway.

## Fallback checks

A declared fallback is checked in two places, and the permission check runs last.

The rules cover what the gateway config and the manifest decide. The fallback is served, priced,
distinct from its route and at least as restricted in data class. `brownout check` catches a bad
pairing before deploy. On a running gateway, an admin can delete a model or change its data class
without any notice to the plugin, so the request rules check the pairing again on each request and
refuse with a 503 naming the rule.

The permission check asks LiteLLM's own `_can_object_call_model` whether both the key and the team
may call the fallback. It runs only when the decision wants a downgrade, so a request below
`degrade_at` never touches it. A refusal serves the primary. The response header and the tier
event carry the outcome, and a `brownout_fallback_rejected` event names the route, the target and
the rejection on its own claim per route and rejection per window. A check that raises anything
other than LiteLLM's `ProxyException` is a platform fault, so the plugin serves the primary and
raises an `entitlement_check_unavailable` incident alongside the rejection event.
