# LiteLLM brownout

A LiteLLM custom logger plugin that keeps each team's spend inside a budget its owner approved.
It raises one event per declared warning tier, moves eligible routes to a validated cheaper model
at a route-declared threshold, and tells the calling application in response headers which route
served and why. LiteLLM 1.101.0 already reserves each request's worst-case cost at authentication,
counts spend atomically across replicas in Redis, reconciles to actual cost and refuses admission
at 100%, so the plugin adds only what configuration cannot express. Those four things are tier
events at arbitrary thresholds, route-declared degradation on a team total, validated fallback
selection, and caller disclosure.

`src/budget_policy/policy.py` holds the entire decision as a pure function. `adapter.py` is the
LiteLLM seam around it.

## Installation

The plugin runs inside the stock `docker.litellm.ai/berriai/litellm:v1.101.0` image as a mounted
package and one dotted callback path. Mount the package, point two environment variables at the
manifest and the price catalog, name the handler in `litellm_settings.callbacks`, then create each
governed team in LiteLLM with a `max_budget` equal to its manifest envelope.

### Mount the package

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

### Minimum gateway config

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
| `fail_closed_budget_enforcement: true` | Decides the envelope boundary for the whole gateway, read from `general_settings` alone ([`proxy/proxy_server.py:2429`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2429)). On, a worst case that does not fit the remaining headroom is refused whole at authentication ([`proxy/spend_tracking/budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141)), and so is a reservation that cannot be written or a spend figure that cannot be verified ([`budget_reservation.py:259`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L259), [`proxy_server.py:2527`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2527)). Off, the hold shrinks to the headroom and the request ships at its full `max_tokens`, so a window can close one request above its envelope. Removing the key gives that soft boundary to every team. `disable_budget_reservation` is a third posture with no hold at all, described under [The envelope boundary](#the-envelope-boundary) |
| `coordination_redis` | The spend counter LiteLLM increments per team. The plugin's tier claim reuses the same Redis, so one crossing is one event across every replica. Without Redis the claim falls back to a per-replica set and logs that once |
| `callbacks` | The dotted path to the handler instance `adapter.py` builds at import |
| `input_cost_per_token`, `output_cost_per_token` on each deployment | What LiteLLM reserves against. `tests/test_config_contract.py` requires them to equal the catalog entry for the same model group |
| `model_info.data_class` | Must equal the catalog's class for the group. The plugin reads the catalog, and the contract test keeps the two aligned |

Three keys must stay absent. `general_settings.alerting` would make LiteLLM's hardcoded 85% and
95% alerter a second owner of tier events, `router_settings.fallbacks` (or its context-window and
content-policy variants) would move a request to another model group after the disclosure header
was decided, and `general_settings.disable_budget_reservation` would remove the hold from every
budget on the gateway. `tests/test_config_contract.py` fails on any of the three, and on a missing
`fail_closed_budget_enforcement`.

### Create the team

Every team in the manifest needs a LiteLLM team whose `max_budget` equals `envelope_usd` and whose
`budget_duration` equals the manifest's, with a model list that includes every declared fallback.
Keys carry no budget of their own, because a key budget would be a second ceiling the manifest
knows nothing about. `lab/setup_lab.py` is the reference implementation and does exactly this:

```json
POST /team/new
{"team_id": "team-a", "team_alias": "team-a", "max_budget": 500.0,
 "budget_duration": "7d", "models": ["route-chat", "route-chat-degraded"]}

POST /key/generate
{"team_id": "team-a", "key_alias": "team-a-app", "max_budget": null, "models": []}
```

The plugin checks the amount on every request and refuses with a 503 and a platform incident when
`team.max_budget` drifts from `envelope_usd`. It cannot see the duration at request time, so a
drifted `budget_duration` has to be caught wherever the team is rendered.

## Configuration reference

### Team manifest

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
| `routes.<model_group>.fallback` | model group name or `null` | The degradation target |
| `routes.<model_group>.degrade_at` | integer in 1..99 or `null` | Requires `fallback`. Joined to the team's tier set, so a downgrade raises an event even at a percentage the team did not list |
| `routes.<model_group>.on_counter_unavailable` | as above | Per-route override of the team posture |

### Price catalog

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

### Fallback validation

A degradation target serves only when every check holds. It is priced, it differs from the primary,
it costs no more on either token dimension and less on at least one, its data class sits at or
above the primary's in `data_class_order`, and LiteLLM's own `_can_object_call_model` permits it for
both the key and the team. Any failed check serves the primary, and both the response header and
the tier event carry `fallback_rejected`. An entitlement check that raises anything other than
LiteLLM's `ProxyException` is a platform fault, so it serves the primary and raises an incident
rather than reading as a narrow allowlist.

### The spend figure a tier compares

Authentication reserves the request's worst-case cost on the team counter, the plugin reads that
counter, subtracts the request's own hold, divides by the envelope, and compares the percentage
against the union of `warn_tiers` and the route's `degrade_at`.

The subtraction matters. The hold is a temporary authorisation and a tier measures consumption, so
a request judged on a counter that includes its own hold would push itself toward its own downgrade
by the size of its reservation. Holds placed by other in-flight requests stay in the figure, because
those requests were admitted and each will produce a real charge. A counter that reads below the
request's own hold floors at zero.

The figure runs ahead of settled spend rather than behind it, so a tier fires early rather than
late.

One event per tier, per team, per policy epoch, per window, across every replica. The claim is a
Redis `SET NX` on `budget_policy:tier:<team>:<epoch>:<window>:<tier>` with a TTL of twice the window.
Correctness comes from the key. The window id is derived from LiteLLM's own `budget_reset_at`
anchoring, so a reset re-arms every tier without a TTL guess.

### The envelope boundary

LiteLLM sizes a worst-case hold during authentication, `fail_closed_budget_enforcement` decides
what happens when that hold does not fit, and every plugin hook runs after that decision. The
plugin reports and degrades below the boundary and owns nothing at it.

| Gateway posture | When the worst case exceeds the headroom | Settled spend at window close |
|---|---|---|
| `fail_closed_budget_enforcement: true` | Refused whole, 429 at authentication ([`budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141)) | At or below the envelope, to the accuracy of the estimate below. Up to one worst case of headroom goes unspent |
| Flag absent or `false` | The hold shrinks to the headroom and the request ships at its full `max_tokens` ([`budget_reservation.py:147`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L147)) | Up to one worst case minus the headroom above the envelope. The next request takes the 429 |
| `disable_budget_reservation: true` | No hold is placed. Admission is a read of the counter that the response updates afterwards ([`user_api_key_auth.py:2708`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/user_api_key_auth.py#L2708), [`auth_checks.py:5242`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/auth_checks.py#L5242)) | Concurrent requests each pass on the same figure, so no single request bounds the overshoot |

Set the flag when the envelope is a ceiling that must hold, and accept that the last fraction of
headroom below one request's worst case goes unspent. Leave it out when spending the whole envelope
matters more than a one-request overshoot, and accept that an unwritable hold or an unverifiable
counter is then admitted rather than refused, for every team on the gateway. The third posture
stays absent.

The worst case is the counted input tokens at the input price plus
`min(max_tokens, model_info.max_output_tokens or 16384)` at the output price, taken as the maximum
over the group's deployments
([`budget_reservation.py:1434-1462`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L1434-L1462)).
Two things settle above that hold. A provider that emits past `max_output_tokens`, and a cost
component the estimate does not price. The router fills `max_output_tokens` from LiteLLM's cost map
for an underlying model the map knows
([`router.py:10339`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/router.py#L10339),
[`router.py:10528-10535`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/router.py#L10528-L10535)),
so the field matters on a deployment the map does not know, such as the lab's mock models, where an
absent field reserves 16384 output tokens.

A per-team hard boundary is buildable in one direction. On a gateway with the flag off, the
pre-call hook could refuse a request whose hold was shrunk, at the cost of duplicating LiteLLM's
worst-case estimate and leaving every team with the flag-off failure rows. On a gateway with the
flag on, the hook sees only requests that were already admitted whole, so it can loosen nothing.
The flag is the whole mechanism today.

### Degradation posture and failure modes

Every failure direction is closed. A request the plugin cannot govern is refused.

| Condition | Response |
|---|---|
| Route reaches its declared threshold | Serve the validated cheaper route with tier and degradation headers |
| Team reaches 100% of its budget | Native 429 at reservation time. The plugin never sees that request |
| Caller supplies `fallbacks`, `mock_testing_fallbacks`, any `x-platform-*` key or `degraded_from` in `metadata` or `litellm_metadata` | 400 `caller_routing_not_permitted` naming the refused keys |
| Requested model has no committed price | 503 `unpriced_model` plus a platform incident |
| Key has no team, or its team is absent from the manifest | 503 `team_not_in_manifest` plus a platform incident |
| Spend counter unreadable | The route's `on_counter_unavailable` posture. `block` is a 503 `spend_counter_unreadable`. `degrade` with no selectable target collapses to `block`. Every posture raises a platform incident |
| Runtime `team.max_budget` drifts from `envelope_usd` | 503 `budget_ceiling_drift` plus a platform incident |
| Degradation target fails validation or entitlement | Serve the primary, disclose `fallback_rejected`, tag the tier event with it |
| Entitlement check itself cannot run | Serve the primary and raise a platform incident |
| Redis unavailable for the tier claim | Deduplication falls back to per-replica and says so in the log |
| Non-chat call type (embeddings, images) | Passed through untouched |

An inbound `_budget_policy` key in request metadata is dropped rather than refused, because the
plugin rewrites it authoritatively on every request and nothing reads a forged value.

A team at 90% of its own envelope is an event, owner `team`. Enforcement becoming unavailable is
the page-worthy condition, and those events carry owner `platform`.

### Response headers

Headers are set in the post-call hook on the success path.

| Header | Present | Value |
|---|---|---|
| `x-platform-route-served` | every governed response | The model group that served |
| `x-platform-deployment-id` | when LiteLLM reports one | The deployment id within that group |
| `x-platform-budget-tier` | when a tier is reached | The highest declared tier at or below current utilisation |
| `x-platform-degraded-from` | when degraded | The route the caller asked for |
| `x-platform-degradation-reason` | when the decision has something to say | `degrade_tier_reached` or `fallback_rejected` |

### Events and metrics

Every event is a structured log line on the `budget_policy` logger, tiers at warning and incidents
at error. With `BUDGET_POLICY_WEBHOOK_URL` set, the same payload is POSTed with a one-second
timeout. With `DD_API_KEY` set, series go to the Datadog v2 API directly. Remote delivery is
concurrent and best-effort, and a failed sink cannot fail the request.

```json
{"event": "budget_tier_crossed", "owner": "team", "team_id": "team-a",
 "policy_epoch": "team-a-v1", "window": "2026-09-21T00:00:00+00:00",
 "tier": 75, "route": "route-chat", "served_route": "route-chat-degraded",
 "decision": "degrade", "reason": "degrade_tier_reached",
 "spend_usd": 376.2, "envelope_usd": 500.0}

{"event": "budget_policy_incident", "owner": "platform",
 "reason": "budget_ceiling_drift", "window": "2026-09-21T00:00:00+00:00",
 "team_id": "team-a", "envelope_usd": 500.0, "team_max_budget_usd": 5000.0,
 "manifest_version": "v1"}
```

Incident reasons are `unpriced_model`, `team_not_in_manifest`, `budget_ceiling_drift`,
`spend_counter_unreadable` and `entitlement_check_unavailable`. Incidents are deduplicated once per
window on their own claim key.

| Metric | Type | Tags |
|---|---|---|
| `budget_policy.tier_crossed` | count | `env`, `service:budget-policy`, `team`, `tier`, `decision`, `reason`, `route` |
| `budget_policy.envelope_utilisation` | gauge | `env`, `service:budget-policy`, `team` |
| `budget_policy.incident` | count | `env`, `service:budget-policy`, `reason`, and `team` when the incident names one |

The utilisation gauge is sampled at tier transitions only. Numbers and tags are all that reach
Datadog, and `tests/test_metrics.py` pins that on the wire.

## Native vs built

```
  PROVIDERS   ══════════════════════════════════════════════════════
                        ▲                              │
                      5 │ dispatch            response │ 6
                        │                              ▼
  LITELLM     ──────────────────────────────────────────────────────
                 4  pre_call hook   ★    │   7  post_call hook   ★
                 3  merge guardrails     │   8  reconcile R to actual
                 2  reserve worst case R │
                 1  authenticate         │
              ──────────────────────────────────────────────────────
                        ▲                              │
                        │ POST                     200 │
                        │ /v1/chat/completions         ▼
  CALLER      ══════════════════════════════════════════════════════

              ★  the two stages this plugin owns
```

Stage 4 is where the plugin decides, and its position sets three constraints.

| Because stage 4 runs | It follows that |
|---|---|
| after stage 2 | the counter it reads already holds a charge for a request that has not run |
| after stage 2 | the request that exhausts the budget never reaches it, so degradation fires below 100% |
| before stage 5 | it cannot know which deployment will serve, so price is a property of the route |

Configuring an existing capability was preferred to reimplementing it. Every native claim below was
read from the pinned source in `.venv/lib/python3.12/site-packages/litellm`.

| Native, configured rather than rebuilt | Where |
|---|---|
| Worst-case cost reserved during authentication, before any callback runs | [`proxy/auth/user_api_key_auth.py:2667`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/user_api_key_auth.py#L2667) |
| The reservation increments the counter with Redis `INCRBYFLOAT` | [`proxy/proxy_server.py:3283`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L3283), [`caching/redis_cache.py:1195`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/caching/redis_cache.py#L1195) |
| Counters for Key, Team and User, only when `max_budget > 0` | [`proxy/spend_tracking/budget_reservation.py:417-474`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L417-L474) |
| An exhausted budget raises `BudgetExceededError` with `status_code = 429` at auth | [`proxy/spend_tracking/budget_reservation.py:161`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L161), [`exceptions.py:440`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/exceptions.py#L440) |
| The hard boundary. With `fail_closed_budget_enforcement` on, a worst case that does not fit the remaining headroom is refused whole at authentication. With it off, the hold is resized to the headroom instead | [`proxy/spend_tracking/budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141), [`budget_reservation.py:147`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L147), [`proxy/proxy_server.py:2429`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2429) |
| Spend reconciled to real cost after the response completes | [`proxy/spend_tracking/budget_reservation.py:305`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L305) |
| `budget_duration: "7d"` anchors the window to Monday at the configured reset time | [`litellm_core_utils/duration_parser.py:246-249`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/litellm_core_utils/duration_parser.py#L246-L249) |
| Model-level guardrails merge into the request just before the pre-call hook | [`proxy/common_request_processing.py:2002`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/common_request_processing.py#L2002) |
| Outage failover across deployments of one route, by `order` and cooldown | [`router.py:12456`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/router.py#L12456) calling `_get_order_filtered_deployments` |

| Built here | Why configuration could not express it |
|---|---|
| Tier events at 75%, 90% and any route-declared threshold | The native alerter is hardcoded to 85% and 95% ([`integrations/SlackAlerting/slack_alerting.py:648-653`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/integrations/SlackAlerting/slack_alerting.py#L648-L653)), and no `alerting` key is declared. A team `soft_budget` is one absolute-dollar threshold, compared against the team row's recorded spend rather than the counter, alert-only, and delivered by email to `metadata.soft_budget_alerting_emails` ([`proxy/auth/auth_checks.py:5332`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/auth_checks.py#L5332), [`proxy/_types.py:1805`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/_types.py#L1805)) |
| Route-declared degradation on a team total | `budget_fallbacks` fires on per-model `model_max_budget`, never on a team total ([`proxy/hooks/model_max_budget_limiter.py:300`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/hooks/model_max_budget_limiter.py#L300)) |
| Validated fallback selection | Nothing native checks that a target is cheaper, priced, and cleared for the primary's data class |
| A catalog that raises on an unknown model | LiteLLM returns `None` for an unpriced model and skips reservation entirely ([`proxy/spend_tracking/budget_reservation.py:236`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L236)) |
| Caller disclosure | No native header carries the served route or the route it degraded from |
| Guardrail re-merge after a rewrite | The merge runs at stage 3 keyed on the requested model, so a stage 4 rewrite would drop the target's guardrails |
| Versioned, cross-replica deduplication | Each claim carries team, policy epoch, window and threshold, so an approved policy change re-arms its transitions |

## Known limitations

Two pieces of work remain.

- Disclosure is not an invariant on blocked requests. LiteLLM wraps the response-headers hook in a
  try/except that swallows, and runs it on success only, so a request blocked at the native ceiling
  carries no platform header and its signal lives in the 429 body. A response middleware seam
  would close this.
- The end-to-end test is serial, so concurrent admission near a threshold is argued in the unit
  suite and has yet to be driven through a live gateway.

Six limits a deployment inherits as the plugin stands.

| Limit | What it means for a deployment |
|---|---|
| Entitlement reads the direct key and team model lists | A target reachable only through an access group, a team alias, or project or member scope reads as unselectable, so the primary is served. Those scopes need LiteLLM's full resolved-model path |
| Only the budget amount is re-checked per request | The authenticated object carries `team_max_budget` and no duration or reset instant ([`proxy/_types.py:2879`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/_types.py#L2879)), so a drifted window is caught where the team is rendered. The manifest parses `budget_duration` at load, because one derivation raises on a bad value and the other quietly returns the next midnight |
| Data classes are compared as an ordering | The whole rule for two classes. Provider, region, retention, purpose and audit requirement can each be more restrictive without the combination being permitted, and a production deployment needs a matrix over those dimensions |
| Money is a `float` | It matches the counter LiteLLM increments with `INCRBYFLOAT`. Nothing here is the system of record for spend |
| The reservation is sized on the requested route, never the served one | Reservation runs at authentication and the rewrite runs after it, so a degraded request still needs the primary's worst case to fit the headroom. With `fail_closed_budget_enforcement` on, admission therefore stops at the envelope minus the requested route's worst case, and a degrade tier above that ratio never fires. Keep `max_tokens` small relative to the envelope, as the lab does |
| A request LiteLLM cannot estimate places no hold | `reserve_budget_for_request` returns before reserving when the worst-case estimate is `None` ([`budget_reservation.py:236-237`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L236-L237)). That is a group the router and the cost map cannot price, an input the token counter cannot count on a model with no `max_input_tokens`, or an image route priced per pixel or by size ([`budget_reservation.py:1222-1228`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L1222-L1228)). The fail-closed flag is consulted only after the estimate, so it changes nothing on this path. Admission falls back to `_team_max_budget_check` ([`auth_checks.py:5242`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/auth_checks.py#L5242)), a plain read of a counter the response updates afterwards ([`proxy_server.py:2713`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2713)), so concurrent requests each pass on the same figure. The catalog proves the plugin knows a price, and LiteLLM estimates from the deployment's own price fields, so `tests/test_config_contract.py` keeps the two equal for every deployment in the lab config. Non-chat call types pass through the plugin untouched, so an embeddings, audio or image route the estimate cannot price is admitted on the read alone |

## Running the tests

```bash
cp .env.example .env          # set LITELLM_MASTER_KEY and the two passwords
uv sync
uv run pytest -m "not e2e"    # 111 tests, no Redis or gateway needed
```

The decision function is pure, and the adapter's two impure edges, the spend counter and the tier
store, are injected, so the unit suite runs from a clean clone. `tests/test_config_contract.py`
holds the cross-file checks that make an unpriced or unserved route a review failure rather than a
runtime one. `ruff format --check`, `ruff check` and `mypy` run in CI alongside it.

The end-to-end sequence drives the composed gateway over HTTP on mock routes, so it spends no
provider money and still exercises real reservations, a real Redis counter and the native ceiling.

```bash
docker compose up -d --wait
set -a && source .env && set +a
uv run pytest -m e2e          # skips itself when the gateway is unreachable
```

`LAB_REQUIRE_STACK=1` turns an unreachable gateway into a failure, which is what CI does. The
sequence provisions the one lab team the manifest names, so two copies cannot run at once.
`lab/setup_lab.py` is rerunnable at about eleven seconds, because a spend reset takes more than
one write. LiteLLM buffers spend per team and flushes it every `PROXY_BATCH_WRITE_AT` seconds, and
a counter cleared in Redis is reseeded from the database row on the next read, so the script
deletes the team, waits out the flush window, clears the counter and the claims, then recreates.

`config/config.yaml` is the lab gateway config. Its routes answer with `mock_response`, and the
`langfuse` and `datadog_metrics` callbacks it names run against the placeholder keys in
`.env.example` without failing a request.

To watch a $1.00 envelope drain by hand:

```bash
KEY=$(uv run python lab/setup_lab.py | grep -oE 'sk-[A-Za-z0-9_-]+' | head -1)
for i in $(seq 1 70); do
  curl -s -D - -o /dev/null -X POST http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"budget-lab-primary","messages":[{"role":"user","content":"hi"}],"max_tokens":40}' \
  | grep -i '^HTTP/\|^x-platform'
done
curl -s localhost:8088/events | jq -c '.events[] | select(.event=="budget_tier_crossed")'
```

Request 37 is the first downgrade, request 51 is the first to carry tier 90, and request 57 takes
LiteLLM's native 429 with $0.966 settled against a $0.0408 estimate.

```
HTTP/1.1 200 OK
x-platform-route-served: budget-lab-degraded
x-platform-budget-tier: 75
x-platform-degraded-from: budget-lab-primary
x-platform-degradation-reason: degrade_tier_reached
```
