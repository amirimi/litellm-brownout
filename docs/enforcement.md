# Enforcement

Each chat request meets LiteLLM first and the plugin second. LiteLLM authenticates the key,
reserves the request's worst-case cost on the team's Redis counter and refuses the request when
that worst case does not fit. LiteLLM 1.95.1 to 1.100 shrink the hold to fit instead, as
[the envelope boundary](#the-envelope-boundary) sets out. The plugin's pre-call hook then runs the request rules,
reads the counter, decides, checks the caller's permission only if it wants a downgrade, rewrites
the request if the downgrade is allowed, announces any claimed transition on the gateway log, and
writes the request's facts into its LiteLLM metadata. The provider answers, the plugin's post-call
hook sets the disclosure headers from those facts, LiteLLM reconciles the hold to the actual cost,
and every LiteLLM logging integration receives the facts with the request's log.

## The spend figure a tier compares

Authentication reserves the request's worst-case cost on the team counter. The plugin reads that
counter, subtracts the request's own hold, divides by the envelope, and compares the percentage
against three independent lists: the team's `warn_tiers`, the route's `warn_tiers` and the route's
`degrade_at`. None is merged into another.

The plugin subtracts the request's own hold because the hold is a temporary authorisation and a
tier measures consumption. A request judged on a counter that includes its own hold would push
itself toward its own downgrade by the size of its reservation. Holds placed by other in-flight
requests stay in the figure, because those requests were admitted and each will produce a real
charge. A counter that reads below the request's own hold floors at zero.

The figure runs ahead of settled spend, so a tier fires early.

Every transition is claimed once per window across every replica, with a Redis `SET NX` and a TTL
of twice the window.

| Transition | Claim key |
|---|---|
| Team tier | `brownout:tier:<team>:<epoch>:<window>:team:<tier>` |
| Route tier | `brownout:tier:<team>:<epoch>:<window>:route:<route>:<tier>` |
| Route degraded | `brownout:degraded:<team>:<epoch>:<window>:<route>` |
| Fallback rejected | `brownout:fallback_rejected:<team>:<epoch>:<window>:<route>:<rejection>` |
| Incident | `brownout:incident:<reason>:<subject>:<window>` |

The window is LiteLLM's next reset instant for the team's `budget_duration`, computed by LiteLLM's
`get_budget_reset_time` with the gateway's `timezone` and `budget_reset_time`, and written in UTC.
A reset therefore re-arms every transition. LiteLLM's reset job zeroes the counter on its own
schedule, every 597 to 605 seconds by default, so for a few minutes after each reset the plugin can
read the previous window's spend under the new window.

## The envelope boundary

LiteLLM sizes a worst-case hold during authentication, `fail_closed_budget_enforcement` decides
what happens when that hold does not fit, and every plugin hook runs after that decision. The
plugin reports and degrades below the boundary, and LiteLLM alone owns the boundary itself.

| Gateway posture | When the worst case exceeds the headroom | Settled spend at window close |
|---|---|---|
| `fail_closed_budget_enforcement: true` on LiteLLM 1.101.0 and newer | Refused whole, 429 at authentication ([`budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141)) | At or below the envelope, to the accuracy of the estimate below. Up to one worst case of headroom goes unspent |
| `fail_closed_budget_enforcement: true` on LiteLLM 1.95.1 to 1.100 | Hold shrunk to the headroom and the request served at its full `max_tokens`. Requests keep being admitted this way until no headroom is left, then refused with a 429 | Above the envelope by the overrun of the shrunk requests, at most one request's cost under serial traffic. The handler logs this boundary at startup |
| Flag absent or `false` | Refused by rule `fail-closed-on` before the plugin decides | Not reached |
| `disable_budget_reservation: true` | Refused by rule `reservation-on` before the plugin decides | Not reached |

The worst case is the counted input tokens at the input price plus
`min(max_tokens, model_info.max_output_tokens or 16384)` at the output price, taken as the maximum
over the group's deployments
([`budget_reservation.py:1434-1462`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L1434-L1462)).
Two things settle above that hold. One is a provider that emits past `max_output_tokens`, and the
other is a cost component the estimate does not price. The router fills `max_output_tokens` from
LiteLLM's cost map for an underlying model the map knows, so the field matters on a deployment the
map does not know, such as the lab's mock models, where an absent field reserves 16384 output
tokens.

## Failure modes

Every failure direction is closed. A request the plugin cannot govern is refused, and the refusal
names its rule.

| Condition | Response |
|---|---|
| Route reaches its declared threshold | Serve the declared fallback with tier and degradation headers, and raise one `brownout_route_degraded` event per route per window |
| Team reaches 100% of its budget | LiteLLM's budget refusal at reservation time, a 429 up to 1.103 and a 422 by default from 1.104. Setting `litellm_settings.budget_exceeded_status_code: 429` restores the 429, as run on the 1.104.2 lab image. The plugin never sees that request |
| Caller supplies `fallbacks`, `mock_testing_fallbacks`, any `x-brownout-*` key or `degraded_from` in `metadata` or `litellm_metadata` | 400 `caller_routing_not_permitted`, rule `no-caller-routing`, naming the refused keys |
| Key has no team, or its team is absent from the manifest | 503 `team_not_in_manifest`, rule `team-in-manifest`, plus a platform incident |
| Key carries its own `max_budget` | 503 `key_has_own_budget`, rule `key-has-no-budget`, plus a platform incident |
| Requested model has no price | 503 `unpriced_model`, rule `model-priced`, plus a platform incident |
| Runtime `team.max_budget` drifts from `envelope_usd` | 503 `budget_ceiling_drift`, rule `ceiling-matches-envelope`, plus a platform incident |
| A gateway setting or a fallback pairing breaks a rule | 503 `gateway_rule_violated` naming the rule and its subject, plus a platform incident |
| Spend counter unreadable | 503 `spend_counter_unreadable`, rule `spend-readable`, plus a platform incident |
| Caller's key or team may not call the fallback | Serve the primary, disclose `fallback_rejected`, and raise one `brownout_fallback_rejected` event per route and rejection per window |
| Permission check itself cannot run | Serve the primary, raise a platform incident and a `brownout_fallback_rejected` event |
| Redis unavailable for a claim | Deduplication falls back to per-replica and says so in the log |
| Non-chat call type (embeddings, images) | Passed through untouched |

A caller-supplied `spend_logs_metadata.brownout` is removed without an error on every request the
plugin sees, chat or not, before anything reads it. On a governed request the plugin then writes its
own.

## Response headers

The post-call hook builds these headers from the request's facts on the success path.

| Header | Present | Value |
|---|---|---|
| `x-brownout-route-served` | every governed response | The model group that served |
| `x-brownout-deployment-id` | when LiteLLM reports one | The deployment id within that group |
| `x-brownout-budget-tier` | when a team tier is reached | The highest team tier at or below current utilisation |
| `x-brownout-route-tier` | when a route tier is reached | The highest tier the requested route declares at or below current utilisation |
| `x-brownout-degraded-from` | when degraded | The route the caller asked for |
| `x-brownout-degradation-reason` | when a downgrade was wanted | `degrade_tier_reached` or `fallback_rejected` |

## Request facts and announcements

The plugin reports through two channels and owns no delivery code of its own. Each governed request
carries facts about itself into LiteLLM's logging, so every integration the gateway already runs
receives them. Each transition the plugin claims for a team's window is announced once on the
gateway log.

| Kind | Channel | Written by | Read by |
|---|---|---|---|
| A fact about one request | `metadata.spend_logs_metadata.brownout` on the request | The plugin on every request it handles | Every LiteLLM logging integration, the spend logs and the response headers |
| An announcement about a team's window | The `brownout` logger on gateway stdout | The plugin only | Whoever reads the gateway's logs, which is where platform alerts belong |

### Request facts

LiteLLM copies `spend_logs_metadata` into the payload every logging callback receives, and into its
spend logs. The plugin writes one `brownout` entry there and keeps any other keys the caller, the
key or the team put beside it.

| Key | Value | Present |
|---|---|---|
| `requested_route` | The model group the caller asked for | Always |
| `outcome` | `served`, `degraded`, `rejected` or `refused` | Always |
| `served_route` | The group the decision routes to | Unless `refused` |
| `budget_tier`, `route_tier` | The highest tier reached, as a string such as `"75"` | When a tier is reached |
| `degradation_reason` | `degrade_tier_reached` or `fallback_rejected` | When a downgrade was wanted |
| `utilisation_percent` | The team's spend as a percentage of its envelope, a number | When the spend was read |
| `refusal` | The refusal reason, such as `key_has_own_budget` | When `refused` |

The entry describes its own request and nothing else. It carries no team, no window and no other
route's state, because every integration already attributes the request to a team through LiteLLM's
`user_api_key_team_id`.

The entry is authoritative on every request the plugin handles. LiteLLM gives a plugin no logging
field that callers cannot write, so on a request LiteLLM refuses before the plugin runs, such as a
budget refusal or a rate limit, the entry is whatever the caller sent. LiteLLM attributes that
request to the caller's own key and team, so a forged entry misleads only the team that owns the
forging key. Build team dashboards and team alerts on the entry, and never platform alerts.

A team derives window-level alerts in its own tool from these facts.

- **A `CustomLogger`:** read `kwargs["standard_logging_object"]["metadata"]["spend_logs_metadata"]["brownout"]` in `async_log_success_event` and `async_log_failure_event`.
- **Prometheus:** set `custom_prometheus_metadata_labels: ["metadata.brownout.outcome", "metadata.brownout.served_route"]` under `litellm_settings` with LiteLLM's `prometheus` callback. LiteLLM flattens `spend_logs_metadata` into the label lookup, so the path omits it. Labels keep string values only, which is why tiers are strings. Verified on 1.95.1 and 1.101.0. Caller metadata of the same name merges first and the plugin's entry overrides it, so on a request the plugin does not govern a caller's `metadata.brownout` can surface as a label for that caller's team.
- **A log-based monitor:** count `outcome:degraded` grouped by `requested_route`, or alert when `budget_tier` reaches `75` grouped by LiteLLM's team field.

Order-based failover logs one request twice, a failure for the attempt that failed and a success for
the one that served. Both logs carry the same entry, so per-request counts follow LiteLLM's own
semantics. A `CustomLogger` that runs after brownout and replaces `spend_logs_metadata` wholesale
drops the entry for that request.

### Announcements

Each transition the plugin claims is one JSON line on the `brownout` logger, team events at warning
and incidents at error. The claim key holds it to once per window across every replica. The line is
written when the pre-call hook claims it, before the request is dispatched. The plugin JSON-encodes
every value, so nothing a caller sends can break out of a line. Match alerts on the logger name
`brownout`, never on the text, because LiteLLM at debug verbosity can log request bodies containing
the same words.

```json
{"event": "brownout_tier_crossed", "owner": "team", "team_id": "team-a",
 "policy_epoch": "team-a-v1", "window": "2026-09-21T00:00:00+00:00",
 "scope": "team", "route": "route-chat", "tier": 75,
 "served_route": "route-chat", "outcome": "served",
 "spend_usd": 376.2, "envelope_usd": 500.0}

{"event": "brownout_route_degraded", "owner": "team", "team_id": "team-a",
 "policy_epoch": "team-a-v1", "window": "2026-09-21T00:00:00+00:00",
 "route": "route-chat", "served_route": "route-chat-degraded", "degrade_at": 90,
 "spend_usd": 452.1, "envelope_usd": 500.0}

{"event": "brownout_fallback_rejected", "owner": "team", "team_id": "team-a",
 "policy_epoch": "team-a-v1", "window": "2026-09-21T00:00:00+00:00",
 "route": "route-chat", "target": "route-chat-degraded",
 "rejection": "outside_caller_allowlist", "degrade_at": 90,
 "spend_usd": 452.1, "envelope_usd": 500.0}

{"event": "brownout_incident", "owner": "platform",
 "reason": "budget_ceiling_drift", "window": "2026-09-21T00:00:00+00:00",
 "rule": "ceiling-matches-envelope", "team_id": "team-a",
 "envelope_usd": 500.0, "team_max_budget_usd": 5000.0}
```

A tier event's `scope` is `team` for a team tier and `route` for a route tier, and its `outcome` is
`served`, `degraded` or `rejected`. A route raises `brownout_route_degraded` the first time it
serves its fallback in a window, on its own claim, so a tier that another route crossed first
cannot hide it.

A rejection is `outside_caller_allowlist` or `entitlement_check_unavailable`. Incident reasons are
`team_not_in_manifest`, `key_has_own_budget`, `unpriced_model`, `budget_ceiling_drift`,
`gateway_rule_violated`, `spend_counter_unreadable` and `entitlement_check_unavailable`, and each
incident names its rule. Incidents are deduplicated once per window on their own claim key. An
incident is the page-worthy condition, and it reaches only the log line.

An `unpriced_model` incident is keyed on the model only when the model is a group the gateway's
config declares, because that is a gap in pricing. Any other name is the caller's choice, so it
raises at most one incident per team per window. Keyed on the name, a key holder could raise an
unbounded number of platform incidents by inventing model names. The incident's
`model_group_served` field says which case it was, and the caller gets the same 503 either way.
