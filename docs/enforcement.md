# Enforcement

Each model call meets LiteLLM first and the plugin second. LiteLLM authenticates the key and
reserves the request's worst-case cost on the team's counter. The plugin's pre-call hook then runs
the request rules, reads the counter, decides, and rewrites the request when a downgrade is allowed.
Its deployment filter verifies the router's choice against that decision, and its post-call hook
sets the disclosure headers. LiteLLM reconciles the hold to the actual cost and passes the
request's facts to every logging integration.

## Call kinds

The pre-call hook sorts each call by LiteLLM's call type into one kind. A call type outside this
table is refused.

| Kind | Endpoints | Checks | Downgrade |
|---|---|---|---|
| `text` | `/v1/chat/completions`, `/v1/completions`, `/v1/messages`, `/v1/responses`, Google's `generateContent` | Team, key, concrete group, per-token price, tiers | Past `degrade_at` |
| `embedding` | `/v1/embeddings` | Team, key, concrete group, per-token price, tiers | Never |
| `compaction` | `/v1/responses/compact` | Team, key, concrete group, per-token price, tiers | Never |
| `other_model` | Images, speech, transcription, moderation, rerank, OCR and video generation | Team, key, concrete group, tiers | Never |
| `no_model` | MCP tool calls, files, vector stores, containers, stored responses, video status and listing, and fine-tuning job listing | Team and key | Not applicable |
| Refused | Provider pass-through, realtime, batches, fine-tuning, search tools, video remixes and edits, and every call type a later release adds | 503 `call_type_not_governed` | |

Embeddings and compaction keep their model, because a swapped embedding model returns vectors from
another space and Codex's compacted summary is read next by the model it named. A chat or responses
call that hands LiteLLM an MCP tool stays one `text` call, so every turn of its tool loop runs on the
model the plugin chose.

## The spend figure a tier compares

The plugin reads the team's counter, subtracts the request's own hold, and divides by the envelope.
It compares that percentage with the team's `warn_tiers`, the route's `warn_tiers` and the route's
`degrade_at`, afresh on every request. Holds of other in-flight requests stay in the figure, so a
tier can fire a little early under concurrency.

Each transition is announced once per budget window across every replica. A window ends at
LiteLLM's next budget reset for the team, which re-arms every announcement.

## The envelope boundary

LiteLLM owns the boundary. With `fail_closed_budget_enforcement` on, a request whose worst case
does not fit the remaining budget gets LiteLLM's 429 before the plugin runs, and settled spend stays
at or below the envelope. The worst case is the counted input at the input price plus `max_tokens`,
capped at `model_info.max_output_tokens` or 16384, at the output price.

## Failure modes

Every call the plugin sees fails closed. A refusal's error body carries its reason and rule.

| Condition | Response |
|---|---|
| Route reaches `degrade_at` | Serve the fallback with tier and degradation headers, and announce `brownout_route_degraded` once per route per window |
| Router fallbacks configured for the gateway, a key or a team | Ignored on governed calls, because the plugin empties every fallback list and sets `disable_fallbacks`. Failover between deployments of one route still works |
| The chosen route returns a provider error | The caller receives the error, and nothing retries the primary |
| Team reaches its budget | LiteLLM's 429, before the plugin sees the request |
| Caller sends `fallbacks`, `context_window_fallbacks`, `content_policy_fallbacks` or `mock_testing_fallbacks` | 400 `caller_routing_not_permitted`, rule `no-caller-routing` |
| Key has no team, or its team is absent from the manifest | 503 `team_not_in_manifest`, rule `team-in-manifest`, plus a platform incident |
| Key carries its own budget | 503 `key_has_own_budget`, rule `key-has-no-budget`, plus a platform incident |
| Requested model is not a configured model group, such as a router alias, a wildcard name, a deployment id or a list | 503 `model_not_a_group`, rule `model-is-a-group`, plus a platform incident |
| A deployment of a `text` or `embedding` model has no per-token price | 503 `unpriced_model`, rule `model-priced`, plus a platform incident |
| Team `max_budget` drifts from `envelope_usd` | 503 `budget_ceiling_drift`, rule `ceiling-matches-envelope`, plus a platform incident |
| A gateway setting breaks a rule | 503 `gateway_rule_violated` naming the rule, plus a platform incident |
| Spend counter unreadable | 503 `spend_counter_unreadable`, rule `spend-readable`, plus a platform incident |
| Unreviewed call type | 503 `call_type_not_governed`, rule `call-type-governed`, plus a platform incident |
| Fallback deleted, unpriced or reclassified after deploy | The primary serves, the response discloses `fallback_rejected`, and `brownout_fallback_rejected` carries `fallback_rule_violated` |
| Caller's key or team may not call the fallback | The primary serves, the response discloses `fallback_rejected`, and `brownout_fallback_rejected` carries `outside_caller_allowlist` |
| Permission check cannot run | The primary serves, with an `entitlement_check_unavailable` incident |
| LiteLLM cannot reach Redis | LiteLLM refuses every budgeted request with a 503 until Redis returns and its circuit breaker resets |
| Redis loses the plugin's claim keys | Every announcement in the current window repeats once |

A caller-supplied `spend_logs_metadata.brownout` is replaced on every request the plugin sees.

## Response headers

The response body's `model` keeps the route the caller asked for. The headers name what served.

| Header | Present | Value |
|---|---|---|
| `x-brownout-route-served` | every response to a call that names a model | The model group that served |
| `x-brownout-deployment-id` | when LiteLLM reports one | The deployment id within that group |
| `x-brownout-budget-tier` | when a team tier is reached | The highest team tier at or below current utilisation |
| `x-brownout-route-tier` | when a route tier is reached | The highest tier the requested route declares at or below current utilisation |
| `x-brownout-degraded-from` | when degraded | The route the caller asked for |
| `x-brownout-degradation-reason` | when a downgrade was wanted | `degrade_tier_reached` or `fallback_rejected` |

A failed provider call carries no `x-brownout-route-served`. LiteLLM's own budget refusal carries
no brownout header, because LiteLLM refuses it before the plugin runs.

## Request facts and announcements

Each request carries facts about itself into LiteLLM's logging, and each transition is announced
once on the gateway log.

| Kind | Channel | Read by |
|---|---|---|
| A fact about one request | `spend_logs_metadata.brownout` in the request's metadata | Every LiteLLM logging integration, the spend logs and the response headers |
| An announcement about a team's window | The `brownout` logger on gateway stdout | Whoever reads the gateway's logs, which is where platform alerts belong |

### Request facts

| Key | Value | Present |
|---|---|---|
| `kind` | `text`, `embedding`, `compaction`, `other_model` or `no_model` | Unless `refused` |
| `requested_route` | The model group the caller asked for | Unless the call names no model |
| `outcome` | `served`, `degraded`, `rejected`, `admitted` for a call without a model, or `refused` | Always |
| `served_route` | The group the decision routes to | When the call names a model and is not `refused` |
| `budget_tier`, `route_tier` | The highest tier reached, as a string such as `"75"` | When a tier is reached |
| `degradation_reason` | `degrade_tier_reached` or `fallback_rejected` | When a downgrade was wanted |
| `utilisation_percent` | The team's spend as a percentage of its envelope | When the spend was read |
| `refusal` | The refusal reason, such as `key_has_own_budget` | When `refused` |

On a request LiteLLM refuses before the plugin runs, the entry is whatever the caller sent. Build
team dashboards on the entry and platform alerts on announcements.

- **A `CustomLogger`:** read `kwargs["standard_logging_object"]["metadata"]["spend_logs_metadata"]["brownout"]` in `async_log_success_event` and `async_log_failure_event`.
- **Prometheus:** set `custom_prometheus_metadata_labels: ["metadata.brownout.outcome", "metadata.brownout.served_route"]` under `litellm_settings` with LiteLLM's `prometheus` callback.
- **A log-based monitor:** count `outcome:degraded` grouped by `requested_route`, or alert when `budget_tier` reaches `75` grouped by LiteLLM's team field.

### Announcements

Each transition is one JSON line on the `brownout` logger, team events at warning and incidents at
error. Match alerts on the logger name, never on the text.

```json
{"event": "brownout_tier_crossed", "owner": "team", "team_id": "team-a",
 "policy_epoch": "132b42eb6dca", "window": "2026-09-21T00:00:00+00:00",
 "scope": "team", "route": "route-chat", "tier": 75,
 "served_route": "route-chat", "outcome": "served",
 "spend_usd": 376.2, "envelope_usd": 500.0}

{"event": "brownout_route_degraded", "owner": "team", "team_id": "team-a",
 "policy_epoch": "132b42eb6dca", "window": "2026-09-21T00:00:00+00:00",
 "route": "route-chat", "served_route": "route-chat-degraded", "degrade_at": 90,
 "spend_usd": 452.1, "envelope_usd": 500.0}

{"event": "brownout_fallback_rejected", "owner": "team", "team_id": "team-a",
 "policy_epoch": "132b42eb6dca", "window": "2026-09-21T00:00:00+00:00",
 "route": "route-chat", "target": "route-chat-degraded",
 "rejection": "outside_caller_allowlist", "degrade_at": 90,
 "spend_usd": 452.1, "envelope_usd": 500.0}

{"event": "brownout_incident", "owner": "platform",
 "reason": "budget_ceiling_drift", "window": "2026-09-21T00:00:00+00:00",
 "rule": "ceiling-matches-envelope", "team_id": "team-a",
 "envelope_usd": 500.0, "team_max_budget_usd": 5000.0}
```

A tier event's `scope` is `team` or `route`, and its `outcome` is `served`, `degraded` or
`rejected`. A rejection is `outside_caller_allowlist`, `entitlement_check_unavailable`,
`fallback_rule_violated` or `agent_key`. A request from an agent key is never downgraded and carries
`agent_key`. Incident reasons are `team_not_in_manifest`, `key_has_own_budget`, `model_not_a_group`,
`unpriced_model`, `budget_ceiling_drift`, `gateway_rule_violated`, `spend_counter_unreadable`,
`call_type_not_governed` and `entitlement_check_unavailable`, each once per window.
