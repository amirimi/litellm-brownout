# Enforcement

Each model call meets LiteLLM first and the plugin second. LiteLLM authenticates the key,
reserves the request's worst-case cost on the team's Redis counter and refuses the request when
that worst case does not fit, as [the envelope boundary](#the-envelope-boundary) sets out. The plugin's pre-call hook then runs the request rules,
reads the counter, decides, checks the caller's permission only if it wants a downgrade, rewrites
the request if the downgrade is allowed, announces any claimed transition on the gateway log, and
writes the request's facts into its LiteLLM metadata. The router then gathers the deployments that
may serve the request, and the plugin's deployment filter refuses it unless every one belongs to the
group the plugin decided and carries a price LiteLLM bills. The provider answers, the plugin's post-call
hook sets the disclosure headers from those facts, LiteLLM reconciles the hold to the actual cost,
and every LiteLLM logging integration receives the facts with the request's log.

## Call kinds

The pre-call hook sorts each call by LiteLLM's call type into one kind and applies that kind's
checks. One table in `bridge.py` lists every admitted call type, and a call type outside it is
refused.

| Kind | Endpoints | Checks | Downgrade |
|---|---|---|---|
| `text` | `/v1/chat/completions`, `/v1/completions`, `/v1/messages`, `/v1/responses`, Google's `generateContent` | Team, key, concrete group, per-token price, tiers | Past `degrade_at` |
| `embedding` | `/v1/embeddings` | Team, key, concrete group, per-token price, tiers | Never |
| `compaction` | `/v1/responses/compact` | Team, key, concrete group, per-token price, tiers | Never |
| `other_model` | Images, speech, transcription, moderation, rerank, OCR and video generation | Team, key, concrete group, tiers | Never |
| `no_model` | MCP tool calls, files, vector stores, containers, reading, deleting and cancelling stored responses, and video status, content and listing | Team and key | Not applicable |
| Refused | Provider pass-through, realtime, batches, fine-tuning, search tools, video remixes and edits, and every call type a later release adds | 503 `call_type_not_governed` | |

A swapped embedding model returns vectors from another space and corrupts the caller's index.
Codex sends a long session to `/v1/responses/compact` and gets back an encrypted summary that the
model it named reads next. Only text generation is therefore ever downgraded. LiteLLM prices the `other_model` endpoints per image,
second, character, query or page, and those prices move between releases, so brownout checks a
per-token price on `text`, `embedding` and `compaction` only.

A chat or responses call that hands LiteLLM an MCP tool to run is one `text` call. LiteLLM runs the
tool loop inside the deployment the router chose, so every turn of the loop stays on the model the
plugin decided. LiteLLM runs no plugin hook on its assistants, threads and runs endpoints, so they
reach no row of this table.

## Assumptions

Brownout governs team budgets on one LiteLLM gateway. Each assumption below either has a rule that
enforces it, named by its id, or is documented and left unchecked. `brownout check` runs the file
rules in CI, and the gateway runs the request rules on every governed request.

| Assumption | When it breaks | Enforced by |
|---|---|---|
| Budgets belong to teams, and every governed key belongs to a manifest team | 503 and a platform incident | `team-in-manifest` |
| Keys carry no budget of their own, whether `max_budget`, `model_max_budget` or `budget_limits` | 503 and a platform incident | `key-has-no-budget` |
| The team's LiteLLM `max_budget` equals `envelope_usd` | 503 and a platform incident | `ceiling-matches-envelope` |
| The team's LiteLLM `budget_duration` equals the manifest's | Tier windows drift from LiteLLM's resets | `duration-matches`, run by `brownout init` |
| The window tumbles with LiteLLM's resets | The manifest refuses to load | `duration-tumbles` |
| No team member carries a budget inside the team | LiteLLM refuses that member before any tier fires | `member-has-no-budget`, run by `brownout init` |
| No user, organisation, end-user or tag budget blocks first | LiteLLM refuses before any tier fires | Documented, not checked |
| No stacked team `budget_limits` | A daily limit inside a weekly one hits with no brownout first | Documented, not checked |
| The team's model list includes each fallback | The primary serves and a `brownout_fallback_rejected` event fires | The permission check |
| `fail_closed_budget_enforcement` is on and reservations are on | 503 and a platform incident | `fail-closed-on`, `reservation-on` |
| A governed request is served only by the route the plugin chose | The plugin empties every router fallback list on each governed request, so gateway, key, team and caller fallbacks never move it. Failover between deployments of one route still works | Enforced on every request |
| LiteLLM's budget alerts are off and `coordination_redis` is configured | `brownout check` fails | `no-native-budget-alerts`, `redis-configured` |
| Every manifest route and fallback is served and priced, and a fallback pairing's deployments declare one data class each | `brownout check` fails before deploy. A pairing that breaks after deploy withholds the downgrade, so the primary serves and the platform gets an incident | `route-served`, `route-priced`, `data-class-declared`, `fallback-data-class` |
| Routes, fallbacks and requests name concrete model groups | An alias, a routing group or a wildcard group reaches its deployments only after the pre-call hook, so the plugin could not govern it | `manifest-schema`, `model-is-a-group` |
| Every call uses a call type the plugin has reviewed | 503 naming the call type, plus a platform incident | `call-type-governed` |
| LiteLLM can price each image, audio, rerank, OCR and video deployment | A deployment LiteLLM cannot price records $0 | Documented, not checked |
| Teams stay off the assistants, threads and runs endpoints | LiteLLM runs no plugin hook there, so that spend skips the tiers | Documented, not checked |
| A fallback serves every endpoint its route's callers use | LiteLLM sends `/v1/messages` to an OpenAI-style deployment as a Responses API call, so a fallback on a server without that API fails with the provider's error | Documented, not checked |
| The reset time occurs exactly once on every reset date | Daylight saving can move a window early or backward | Documented, not checked |
| `degrade_at` sits below 100% minus one worst-case request | The 429 arrives before the downgrade. A request without `max_tokens` reserves the model's whole output | Documented, not checked |
| LiteLLM is 1.101.6 or newer | The gateway refuses to start. It warns past the tested 1.104 | `supported-litellm` |

## The spend figure a tier compares

Authentication reserves the request's worst-case cost on the team counter. The plugin reads that
counter, subtracts the request's own hold, divides by the envelope, and compares the percentage
against three independent lists: the team's `warn_tiers`, the route's `warn_tiers` and the route's
`degrade_at`. None is merged into another. The plugin decides afresh on every request and keeps no
downgrade state between requests.

The plugin subtracts the request's own hold because the hold is a temporary authorisation and a
tier measures consumption. A request judged on a counter that includes its own hold would push
itself toward its own downgrade by the size of its reservation. Holds placed by other in-flight
requests stay in the figure, because those requests were admitted and each will produce a real
charge. A counter that reads below the request's own hold floors at zero.

The figure runs ahead of settled spend, so a tier fires early.

Every transition is claimed once per window across every replica, with a Redis `SET NX` and a TTL
of twice the window. The epoch in each key is derived from the team's policy, so a policy change
re-arms that team's transitions.

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
| `fail_closed_budget_enforcement: true` | Refused whole, 429 at authentication ([`budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141)) | At or below the envelope, to the accuracy of the estimate below. Up to one worst case of headroom goes unspent |
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

Every call the plugin sees fails closed. A call the plugin cannot govern is refused, and the
refusal names its rule.

| Condition | Response |
|---|---|
| Route reaches its declared threshold | Serve the declared fallback with tier and degradation headers, and announce `brownout_route_degraded` once per route per window |
| A router fallback is configured for the gateway, a key or a team | Ignored on every governed request, because the plugin empties `fallbacks`, `context_window_fallbacks` and `content_policy_fallbacks` and sets `disable_fallbacks` before dispatch. The last one also stops the retry LiteLLM's local rate limiter makes on a router fallback, so a rate-limited request gets LiteLLM's 429. Failover between deployments of one route still works |
| The route the plugin chose returns a provider error | The caller receives the provider's error, and nothing retries the primary |
| Team reaches 100% of its budget | LiteLLM's budget refusal at reservation time, a 429 up to 1.103 and a 422 by default from 1.104. Setting `litellm_settings.budget_exceeded_status_code: 429` restores the 429, as run on the 1.104.2 lab image. The plugin never sees that request |
| Caller supplies `fallbacks`, `context_window_fallbacks`, `content_policy_fallbacks` or `mock_testing_fallbacks` | 400 `caller_routing_not_permitted`, rule `no-caller-routing`, naming the refused keys |
| Key has no team, or its team is absent from the manifest | 503 `team_not_in_manifest`, rule `team-in-manifest`, plus a platform incident |
| Key carries its own `max_budget` | 503 `key_has_own_budget`, rule `key-has-no-budget`, plus a platform incident |
| Requested model is not a configured model group, such as a router alias, a routing group, a name a wildcard group serves, a deployment id or a list of models | 503 `model_not_a_group`, rule `model-is-a-group`, plus a platform incident |
| Requested model of a `text` or `embedding` call has a deployment without a per-token price LiteLLM bills | 503 `unpriced_model`, rule `model-priced`, plus a platform incident |
| The router's candidate deployments leave the group the plugin decided, or one has no price | 503 from the deployment filter with the same reason and rule, which LiteLLM places in `error.provider_specific_fields` |
| Runtime `team.max_budget` drifts from `envelope_usd` | 503 `budget_ceiling_drift`, rule `ceiling-matches-envelope`, plus a platform incident |
| A gateway setting breaks a rule | 503 `gateway_rule_violated` naming the rule and its subject, plus a platform incident |
| A fallback pairing breaks a rule after deploy, because the fallback was deleted, unpriced or reclassified | Below `degrade_at`, nothing changes. When a downgrade is wanted, the primary serves, the response discloses `fallback_rejected`, `brownout_fallback_rejected` carries `fallback_rule_violated`, and a `gateway_rule_violated` incident names the rule once per window. The permission check does not run |
| Spend counter unreadable | 503 `spend_counter_unreadable`, rule `spend-readable`, plus a platform incident |
| Caller's key or team may not call the fallback | Serve the primary, disclose `fallback_rejected`, and announce `brownout_fallback_rejected` once per route and rejection per window |
| Permission check itself cannot run | Serve the primary, and announce a platform incident and `brownout_fallback_rejected` |
| LiteLLM cannot write a reservation to Redis | LiteLLM refuses every budgeted request on the gateway with a 503, governed or not, before the plugin runs, because `fail_closed_budget_enforcement` is on. Once Redis is back, LiteLLM keeps refusing until its Redis circuit breaker resets after `REDIS_CIRCUIT_BREAKER_RECOVERY_TIMEOUT`, 60 seconds by default. Run Redis with failover |
| Redis unavailable for a claim | Deduplication falls back to per-replica and says so in the log |
| Redis loses the plugin's claim keys, through a flush or eviction | Every announcement in the current window repeats once. Run Redis with persistence and the `noeviction` policy |
| A call type outside the reviewed kinds, such as a provider pass-through route, realtime, batches or a call type a later LiteLLM release adds | 503 `call_type_not_governed`, rule `call-type-governed`, naming the call type, plus a platform incident once per team and call type per window |

A caller-supplied `spend_logs_metadata.brownout` is removed without an error on every request the
plugin sees, before anything reads it. The plugin then writes its own into the container LiteLLM
keeps its metadata in for that request, `litellm_metadata` on `/v1/messages` and `/v1/responses` and
`metadata` elsewhere.

## Response headers

The post-call hook builds these headers from the request's facts. When a non-streamed provider call
fails, the hook omits `x-brownout-route-served` and `x-brownout-deployment-id`, because nothing
served, and keeps the headers that describe the decision. LiteLLM 1.104.2 calls the hook for a
stream before it reads the first chunk. A stream that fails on its first chunk therefore answers
with an error status and still names the group it was sent to in `x-brownout-route-served`.
LiteLLM's own budget refusal carries no header, because LiteLLM refuses it during authentication,
before the plugin writes the request's facts.

The response body's `model` keeps the route the caller asked for, also after a downgrade. Callers
learn the group that served from `x-brownout-route-served`, which LiteLLM's own
`x-litellm-model-group` header matches.

| Header | Present | Value |
|---|---|---|
| `x-brownout-route-served` | every response to a call that names a model | The model group that served |
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
| A fact about one request | `spend_logs_metadata.brownout` in the request's metadata | The plugin on every request it handles | Every LiteLLM logging integration, the spend logs and the response headers |
| An announcement about a team's window | The `brownout` logger on gateway stdout | The plugin only | Whoever reads the gateway's logs, which is where platform alerts belong |

### Request facts

LiteLLM copies `spend_logs_metadata` into the payload every logging callback receives, and into its
spend logs. The plugin writes one `brownout` entry there and keeps any other keys the caller, the
key or the team put beside it.

| Key | Value | Present |
|---|---|---|
| `kind` | `text`, `embedding`, `compaction`, `other_model` or `no_model` | Unless `refused` |
| `requested_route` | The model group the caller asked for | Unless the call names no model |
| `outcome` | `served`, `degraded`, `rejected`, `admitted` for a call without a model, or `refused` | Always |
| `served_route` | The group the decision routes to | When the call names a model and is not `refused` |
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
- **Prometheus:** set `custom_prometheus_metadata_labels: ["metadata.brownout.outcome", "metadata.brownout.served_route"]` under `litellm_settings` with LiteLLM's `prometheus` callback. LiteLLM flattens `spend_logs_metadata` into the label lookup, so the path omits it. Labels keep string values only, which is why tiers are strings. Verified on 1.101.0. Caller metadata of the same name merges first and the plugin's entry overrides it, so on a request the plugin does not govern a caller's `metadata.brownout` can surface as a label for that caller's team.
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

A tier event's `scope` is `team` for a team tier and `route` for a route tier, and its `outcome` is
`served`, `degraded` or `rejected`. A route raises `brownout_route_degraded` the first time it
serves its fallback in a window, on its own claim, so a tier that another route crossed first
cannot hide it.

A rejection is `outside_caller_allowlist`, `entitlement_check_unavailable` or
`fallback_rule_violated`. Incident reasons are
`team_not_in_manifest`, `key_has_own_budget`, `model_not_a_group`, `unpriced_model`, `budget_ceiling_drift`,
`gateway_rule_violated`, `spend_counter_unreadable`, `call_type_not_governed` and
`entitlement_check_unavailable`, and each
incident names its rule. Incidents are deduplicated once per window on their own claim key. An
incident is the page-worthy condition, and it reaches only the log line.

An `unpriced_model` incident is keyed on the model group, because an unpriced deployment is a gap
in pricing. A name that is not a group is the caller's choice, so its `model_not_a_group` incident
is raised at most once per team per window. Keyed on the name, a key holder could raise an unbounded
number of platform incidents by inventing model names.
