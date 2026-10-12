# Design

The plugin owns two stages of a LiteLLM request and configures the rest. A caller's request is
authenticated, its worst case is reserved and its guardrails are merged. The plugin's pre-call hook
then decides, the provider serves, the plugin's post-call hook discloses, and LiteLLM reconciles
the reservation to the actual cost.

## Where the plugin sits

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
| after stage 2 | on LiteLLM 1.101 and newer the request that exhausts the budget never reaches it, so degradation fires below 100%. On 1.95.1 to 1.100 requests keep arriving with their holds shrunk until no headroom is left, and each is judged like any other |
| before stage 5 | it cannot know which deployment will serve, so price is a property of the route |

## Modules

A request passes through four layers in this order. The bridge turns LiteLLM's request into plain
facts, the request rules refuse anything the plugin cannot govern, and the pure core decides. The
bridge then applies the decision, the transitions are claimed and announced on the log, and the
bridge writes the request's facts into its LiteLLM metadata.

| Module | Holds | Imports LiteLLM |
|---|---|---|
| `policy.py` | `decide` and `resolve`, the only place a threshold is compared, and the names of each outcome | No |
| `manifest.py` | The manifest types and loader, with strict keys and the accepted windows | No |
| `rules.py` | Every rule as a function of plain values, shared by `brownout check` and the request path | No |
| `refusals.py` | Rule ids and the closed `Refusal` type | No |
| `ports.py` | Protocols for every outside dependency, and the `Caller` facts | No |
| `transitions.py` | Window ids, claim keys and one claim-then-announce function | No |
| `events.py` | Event types and the `brownout` log line | No |
| `settings.py` | Environment variables | No |
| `cli.py` | `brownout check` | Its cost map only |
| `bridge.py` | Every LiteLLM touchpoint, including the three private helpers, the version check and the request facts field | Yes |
| `gateway.py` | The two hooks, the request pipeline and the one place a refusal becomes an `HTTPException` | Through the bridge |

The pairing rules and the permission check run only when `decide` wants a downgrade, because they
are the checks whose answers the decision may not need. A broken pairing withholds the downgrade and
skips the permission check. Every other check runs before `decide`, which therefore takes only a
team, a route and a spend figure, and performs no I/O.

## Native vs built

Configuring an existing capability was preferred to reimplementing it. Every native claim below was
read from the LiteLLM 1.101.0 source that the links cite. The hard boundary is the one claim that
differs on the supported releases before 1.101.0.

| Native, configured and reused | Where |
|---|---|
| Worst-case cost reserved during authentication, before any callback runs | [`proxy/auth/user_api_key_auth.py:2667`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/user_api_key_auth.py#L2667) |
| The reservation increments the counter with Redis `INCRBYFLOAT` | [`proxy/proxy_server.py:3283`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L3283), [`caching/redis_cache.py:1195`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/caching/redis_cache.py#L1195) |
| Counters for Key, Team and User, only when `max_budget > 0` | [`proxy/spend_tracking/budget_reservation.py:417-474`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L417-L474) |
| An exhausted budget raises `BudgetExceededError` with `status_code = 429` at auth | [`proxy/spend_tracking/budget_reservation.py:161`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L161), [`exceptions.py:972`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/exceptions.py#L972) |
| The hard boundary. With `fail_closed_budget_enforcement` on, a worst case that does not fit the remaining headroom is refused whole at authentication. With it off, the hold is resized to the headroom instead. On 1.95.1 to 1.100 the hold is resized whatever the flag says, because the refusal arrived in 1.101.0 | [`proxy/spend_tracking/budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141), [`budget_reservation.py:147`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L147), [`proxy/proxy_server.py:2429`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2429) |
| Spend reconciled to real cost after the response completes | [`proxy/spend_tracking/budget_reservation.py:305`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L305) |
| `budget_duration: "7d"` anchors the window to Monday at the configured reset time | [`litellm_core_utils/duration_parser.py:246-249`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/litellm_core_utils/duration_parser.py#L246-L249) |
| Model-level guardrails merge into the request just before the pre-call hook | [`proxy/common_request_processing.py:2002`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/common_request_processing.py#L2002) |
| Outage failover across deployments of one route, by `order` and cooldown | [`router.py:12456`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/router.py#L12456) calling `_get_order_filtered_deployments` |

| Built here | Why configuration could not express it |
|---|---|
| Tier events at 75%, 90% and any route-declared threshold | The native alerter is hardcoded to 85% and 95% ([`integrations/SlackAlerting/slack_alerting.py:648-653`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/integrations/SlackAlerting/slack_alerting.py#L648-L653)), and no `alerting` key is declared. A team `soft_budget` is one absolute-dollar threshold. It compares against the recorded spend on the team row, a different figure from the counter, only alerts, and emails `metadata.soft_budget_alerting_emails` ([`proxy/auth/auth_checks.py:5332`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/auth_checks.py#L5332), [`proxy/_types.py:1805`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/_types.py#L1805)) |
| Route-declared degradation on a team total | `budget_fallbacks` fires on per-model `model_max_budget`, never on a team total ([`proxy/hooks/model_max_budget_limiter.py:300`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/hooks/model_max_budget_limiter.py#L300)) |
| Validated fallback selection | Nothing native checks that a target is priced, cleared for the primary's data class, and callable by the caller's key and team |
| A refusal for a model LiteLLM cannot price | LiteLLM returns `None` for an unpriced model and skips reservation entirely ([`proxy/spend_tracking/budget_reservation.py:236`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L236)) |
| Rules over LiteLLM's own config, run in CI and on every request | LiteLLM validates its settings one at a time and has no notion of the assumptions a plugin makes about them |
| Caller disclosure | No native header or logging field carries the served route or the route it degraded from |
| Guardrail re-merge after a rewrite | The merge runs at stage 3 keyed on the requested model, so a stage 4 rewrite would drop the target's guardrails |
| Versioned, cross-replica deduplication | Each claim carries team, policy epoch, window and transition, so an approved policy change re-arms its transitions |

## Known limitations

Two pieces of work remain.

- A request LiteLLM refuses at its budget ceiling carries no `x-brownout-*` header. LiteLLM refuses
  it during authentication, before the plugin's pre-call hook writes the request's facts, so the
  signal for that request lives in the body of LiteLLM's budget refusal. A response middleware seam
  would close this.
- The end-to-end test is serial, so concurrent admission near a threshold is argued in the unit
  suite and has yet to be driven through a live gateway.

A deployment inherits these limits as the plugin stands. The [assumptions table](../README.md#assumptions)
lists the ones a rule enforces.

| Limit | What it means for a deployment |
|---|---|
| LiteLLM 1.95.1 to 1.100 can close a window above the envelope | Their `_apply_over_budget_reservation_policy` shrinks a hold that does not fit instead of refusing it, and admits requests until no headroom is left. Each admitted request can overrun its shrunk hold. That is at most one request's cost under serial traffic, and more when several shrunk requests are in flight at once. LiteLLM overwrites the hold's original size, so the plugin cannot tell a shrunk hold from a full one, and it logs a warning at startup instead. Run 1.101 or newer for the hard boundary |
| Request compatibility between a route and its fallback goes unchecked | Context capacity, tools, structured output and modalities can differ. With `drop_params: true`, as in the lab config, a parameter the fallback does not support may be dropped silently. Check each fallback against the workload before it carries real traffic |
| The counter resets after the window does | LiteLLM's reset job zeroes the counter every 597 to 605 seconds by default, and later if the job is delayed. Until it runs, the previous window's spend can degrade requests and consume the new window's claims. A team that ends a window above a tier therefore gets false announcements at the next boundary, and none when it really crosses that tier later in the window. Closing this needs the team's persisted `budget_reset_at` |
| Router fallbacks never apply to governed chat completions | The plugin empties every fallback list on each governed request, so a gateway, key or team that relies on cross-group fallbacks for outage resilience loses them on chat. Failover between deployments of one route by `order` and cooldown still works |
| A fallback that errors fails the request | The caller receives the provider's error, and nothing retries the primary. When a large team crosses `degrade_at`, all its traffic for that route moves to the fallback at once, so the provider quota behind the fallback has to absorb the jump |
| Losing Redis keys repeats announcements | A flush, or an eviction policy such as `volatile-lru` that evicts keys with a TTL first, removes the plugin's claims, and every transition in the window is announced again |
| A LiteLLM release that renames a private helper stops startup | The bridge imports three private helpers when LiteLLM loads the callback, so a rename fails the import and the gateway does not start. Pin the image tag and upgrade only to a release the `litellm matrix` workflow has passed |
| Routing can alternate near a threshold under concurrency | Each decision reads a counter that includes the worst-case holds of in-flight requests, which settle lower. Near `degrade_at`, primary and degraded responses can interleave until settled spend passes the threshold |
| Reset times that daylight saving skips or repeats are unsupported | A `budget_reset_time` that does not occur exactly once on a reset date, such as 02:30 or 01:30 in a US zone, makes LiteLLM's own schedule move early or backward on the transition day |
| Only the budget amount is re-checked per request | The authenticated object carries `team_max_budget` and no duration or reset instant ([`proxy/_types.py:2879`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/_types.py#L2879)), so a drifted window is caught where the team is rendered |
| Other owners' LiteLLM budgets are not modelled | A user, organisation, end-user or tag budget, or a stacked team `budget_limits` window, can refuse a request before any tier fires. The authenticated object exposes the user, organisation and end-user budgets per request, and they stay unchecked because refusing them would break gateways that use them for other governance. Team `budget_limits` and tag budgets are not exposed per request |
| Entitlement reads the direct key and team model lists | A target reachable only through an access group, a team alias, or project or member scope reads as unselectable, so the primary is served. Those scopes need LiteLLM's full resolved-model path |
| Data classes are compared as an ordering | The whole rule for two classes. Provider, region, retention, purpose and audit requirement can each be more restrictive without the combination being permitted, and a production deployment needs a matrix over those dimensions |
| Money is a `float` | It matches the counter LiteLLM increments with `INCRBYFLOAT`. Nothing here is the system of record for spend |
| The reservation is sized on the requested route, never the served one | Reservation runs at authentication and the rewrite runs after it, so a degraded request still needs the primary's worst case to fit the headroom. Admission therefore stops at the envelope minus the requested route's worst case, and a degrade threshold above that ratio never fires. Keep `max_tokens` small relative to the envelope, as the lab does |
| A request LiteLLM cannot estimate places no hold | `reserve_budget_for_request` returns before reserving when the worst-case estimate is `None` ([`budget_reservation.py:236-237`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L236-L237)). Rule `model-priced` refuses a chat request for a model LiteLLM cannot price. Non-chat call types pass through the plugin untouched, so an embeddings, audio or image route the estimate cannot price is admitted on LiteLLM's read alone |
| The request rules cost about 0.1 to 0.3 ms | The pre-call hook reads the requested model group through `Router.get_model_group_info`, at about 0.1 ms on the lab config, and the fallback group too when a downgrade is wanted |
