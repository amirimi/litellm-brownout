# Enforcement

Each chat request meets LiteLLM first and the plugin second. LiteLLM authenticates the key,
reserves the request's worst-case cost on the team's Redis counter and refuses the request with a
429 when that worst case does not fit. The plugin's pre-call hook then reads the counter, compares
the team's spend against its tiers, and either leaves the request alone or rewrites it to the
route's fallback. The provider answers, the plugin's post-call hook sets the disclosure headers,
and LiteLLM reconciles the hold to the actual cost.

## The spend figure a tier compares

Authentication reserves the request's worst-case cost on the team counter. The plugin reads that
counter, subtracts the request's own hold, divides by the envelope, and compares the percentage
against the union of `warn_tiers` and the route's `degrade_at`.

The plugin subtracts the request's own hold because the hold is a temporary authorisation and a
tier measures consumption. A request judged on a counter that includes its own hold would push
itself toward its own downgrade by the size of its reservation. Holds placed by other in-flight
requests stay in the figure, because those requests were admitted and each will produce a real
charge. A counter that reads below the request's own hold floors at zero.

The figure runs ahead of settled spend, so a tier fires early.

One event fires per tier, per team, per policy epoch, per window, across every replica. The claim
is a Redis `SET NX` on `budget_policy:tier:<team>:<epoch>:<window>:<tier>` with a TTL of twice the
window. Correctness comes from the key. The window id is derived from LiteLLM's own
`budget_reset_at` anchoring, so a reset re-arms every tier without a TTL guess.

## The envelope boundary

LiteLLM sizes a worst-case hold during authentication, `fail_closed_budget_enforcement` decides
what happens when that hold does not fit, and every plugin hook runs after that decision. The
plugin reports and degrades below the boundary, and LiteLLM alone owns the boundary itself.

| Gateway posture | When the worst case exceeds the headroom | Settled spend at window close |
|---|---|---|
| `fail_closed_budget_enforcement: true` | Refused whole, 429 at authentication ([`budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141)) | At or below the envelope, to the accuracy of the estimate below. Up to one worst case of headroom goes unspent |
| Flag absent or `false` | The hold shrinks to the headroom and the request ships at its full `max_tokens` ([`budget_reservation.py:147`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L147)) | Up to one worst case minus the headroom above the envelope. The next request takes the 429 |
| `disable_budget_reservation: true` | No hold is placed. Admission is a read of the counter that the response updates afterwards ([`user_api_key_auth.py:2708`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/user_api_key_auth.py#L2708), [`auth_checks.py:5242`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/auth_checks.py#L5242)) | Concurrent requests each pass on the same figure, so no single request bounds the overshoot |

Set the flag when the envelope is a ceiling that must hold, and accept that the last fraction of
headroom below one request's worst case goes unspent. Leave it out when spending the whole envelope
matters more than a one-request overshoot. Every team on the gateway then has an unwritable hold or
an unverifiable counter admitted instead of refused. The third posture stays absent.

The worst case is the counted input tokens at the input price plus
`min(max_tokens, model_info.max_output_tokens or 16384)` at the output price, taken as the maximum
over the group's deployments
([`budget_reservation.py:1434-1462`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L1434-L1462)).
Two things settle above that hold. One is a provider that emits past `max_output_tokens`, and the
other is a cost component the estimate does not price. The router fills `max_output_tokens` from
LiteLLM's cost map for an underlying model the map knows
([`router.py:10339`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/router.py#L10339),
[`router.py:10528-10535`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/router.py#L10528-L10535)).
The field therefore matters on a deployment the map does not know, such as the lab's mock models,
where an absent field reserves 16384 output tokens.

A per-team hard boundary is buildable in one direction. On a gateway with the flag off, the
pre-call hook could refuse a request whose hold was shrunk, at the cost of duplicating LiteLLM's
worst-case estimate and leaving every team with the flag-off failure rows. On a gateway with the
flag on, the hook sees only requests that were already admitted whole, so it can loosen nothing.
The flag is the whole mechanism today.

## Failure modes

Every failure direction is closed. A request the plugin cannot govern is refused.

| Condition | Response |
|---|---|
| Route reaches its declared threshold | Serve the declared fallback with tier and degradation headers, and raise one `budget_route_degraded` event per route per window |
| Team reaches 100% of its budget | Native 429 at reservation time. The plugin never sees that request |
| Caller supplies `fallbacks`, `mock_testing_fallbacks`, any `x-platform-*` key or `degraded_from` in `metadata` or `litellm_metadata` | 400 `caller_routing_not_permitted` naming the refused keys |
| Requested model has no committed price | 503 `unpriced_model` plus a platform incident |
| Key has no team, or its team is absent from the manifest | 503 `team_not_in_manifest` plus a platform incident |
| Spend counter unreadable | The route's `on_counter_unavailable` posture. `block` is a 503 `spend_counter_unreadable`. `degrade` with no selectable target collapses to `block`. Every posture raises a platform incident |
| Runtime `team.max_budget` drifts from `envelope_usd` | 503 `budget_ceiling_drift` plus a platform incident |
| Declared fallback is unpriced, is its own route, or carries a less restricted data class | The handler refuses to load, so the gateway does not start |
| Caller's key or team may not call the fallback | Serve the primary, disclose `fallback_rejected`, tag the tier event with it, and raise one `budget_fallback_rejected` event per route and rejection per window |
| Entitlement check itself cannot run | Serve the primary, raise a platform incident and a `budget_fallback_rejected` event |
| Redis unavailable for the tier claim | Deduplication falls back to per-replica and says so in the log |
| Non-chat call type (embeddings, images) | Passed through untouched |

An inbound `_budget_policy` key in request metadata is dropped without an error. The plugin
rewrites that key authoritatively on every request, and nothing reads a forged value.

A team at 90% of its own envelope is an event with owner `team`. Enforcement becoming unavailable
is the page-worthy condition, and those events carry owner `platform`.

## Response headers

The post-call hook sets these headers on the success path.

| Header | Present | Value |
|---|---|---|
| `x-platform-route-served` | every governed response | The model group that served |
| `x-platform-deployment-id` | when LiteLLM reports one | The deployment id within that group |
| `x-platform-budget-tier` | when a tier is reached | The highest declared tier at or below current utilisation |
| `x-platform-degraded-from` | when degraded | The route the caller asked for |
| `x-platform-degradation-reason` | when the decision has something to say | `degrade_tier_reached` or `fallback_rejected` |

## Events and metrics

Every event is a structured log line on the `budget_policy` logger, with team-owned events at
warning and incidents at error. With `BUDGET_POLICY_WEBHOOK_URL` set, the same payload is POSTed
with a one-second timeout. With `DD_API_KEY` set, series go to the Datadog v2 API directly. Remote
delivery is concurrent and best-effort. A failed sink cannot fail the request, and a failed
delivery is dropped without a retry. Events are
published after the request is rewritten and before it is dispatched, so an event describes the
routing decision before any provider has answered.

```json
{"event": "budget_tier_crossed", "owner": "team", "team_id": "team-a",
 "policy_epoch": "team-a-v1", "window": "2026-09-21T00:00:00+00:00",
 "tier": 75, "route": "route-chat", "served_route": "route-chat-degraded",
 "decision": "degrade", "reason": "degrade_tier_reached",
 "spend_usd": 376.2, "envelope_usd": 500.0}

{"event": "budget_route_degraded", "owner": "team", "team_id": "team-a",
 "policy_epoch": "team-a-v1", "window": "2026-09-21T00:00:00+00:00",
 "route": "route-chat", "served_route": "route-chat-degraded", "tier": 90,
 "spend_usd": 452.1, "envelope_usd": 500.0}

{"event": "budget_fallback_rejected", "owner": "team", "team_id": "team-a",
 "policy_epoch": "team-a-v1", "window": "2026-09-21T00:00:00+00:00",
 "route": "route-chat", "target": "route-chat-degraded",
 "rejection": "outside_caller_allowlist", "tier": 90,
 "spend_usd": 452.1, "envelope_usd": 500.0}

{"event": "budget_policy_incident", "owner": "platform",
 "reason": "budget_ceiling_drift", "window": "2026-09-21T00:00:00+00:00",
 "team_id": "team-a", "envelope_usd": 500.0, "team_max_budget_usd": 5000.0,
 "manifest_version": "v1"}
```

A route raises `budget_route_degraded` the first time it serves its fallback in a window, on its
own claim, so a tier that another route crossed first cannot hide it. The team that calls the route
alerts on the metric filtered to that route.

```
sum:budget_policy.route_degraded{route:route-chat}.as_count() > 0
```

A rejection is `outside_caller_allowlist` or `entitlement_check_unavailable`. Every other problem
with a pairing stops the handler from loading.

Incident reasons are `unpriced_model`, `team_not_in_manifest`, `budget_ceiling_drift`,
`spend_counter_unreadable` and `entitlement_check_unavailable`. Incidents are deduplicated once per
window on their own claim key.

| Metric | Type | Tags |
|---|---|---|
| `budget_policy.tier_crossed` | count | `env`, `service:budget-policy`, `team`, `tier`, `decision`, `reason`, `route` |
| `budget_policy.envelope_utilisation` | gauge | `env`, `service:budget-policy`, `team` |
| `budget_policy.route_degraded` | count | `env`, `service:budget-policy`, `team`, `route`, `served_route` |
| `budget_policy.fallback_rejected` | count | `env`, `service:budget-policy`, `team`, `route`, `rejection` |
| `budget_policy.incident` | count | `env`, `service:budget-policy`, `reason`, and `team` when the incident names one |

The utilisation gauge is sampled at tier transitions only. Numbers and tags are all that reach
Datadog, and `tests/test_metrics.py` pins that on the wire.
