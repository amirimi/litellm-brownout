# Design

The plugin owns three stages of a LiteLLM request and configures the rest. A caller's request is
authenticated, its worst case is reserved and its guardrails are merged. The plugin's pre-call hook
then decides, the router gathers the deployments that may serve, and the plugin's deployment filter
verifies them against the decision. The provider serves, the plugin's post-call hook discloses, and
LiteLLM reconciles the reservation to the actual cost.

## Where the plugin sits

```
  PROVIDERS   ══════════════════════════════════════════════════════
                        ▲                              │
                      6 │ dispatch            response │ 7
                        │                              ▼
  LITELLM     ──────────────────────────────────────────────────────
                 5  deployment filter ★  │   8  post_call hook   ★
                 4  pre_call hook   ★    │   9  reconcile R to actual
                 3  merge guardrails     │
                 2  reserve worst case R │
                 1  authenticate         │
              ──────────────────────────────────────────────────────
                        ▲                              │
                        │ POST                     200 │
                        │ /v1/chat/completions         ▼
  CALLER      ══════════════════════════════════════════════════════

              ★  the three stages this plugin owns
```

Stage 4 is where the plugin decides, and its position sets three constraints.

| Because stage 4 runs | It follows that |
|---|---|
| after stage 2 | the counter it reads already holds a charge for a request that has not run |
| after stage 2 | the request that exhausts the budget never reaches it, so degradation fires below 100% |
| before stage 5 | it cannot know which deployments the router will choose, so stage 5 checks each one against the decision and its billed price |

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
| `cli.py` | `brownout check` and `brownout init` | Through the bridge, which prices the config's deployments with LiteLLM's router |
| `bridge.py` | Every LiteLLM touchpoint, including the private helpers `_can_object_call_model`, `_resolve_key_models_for_auth_check` and `_check_and_merge_model_level_guardrails`, the version check, the table of admitted call types and the request facts field | Yes |
| `gateway.py` | The three hooks, the request pipeline per call kind and the one place a refusal becomes an `HTTPException` | Through the bridge |

The pairing rules and the permission check run only when `decide` wants a downgrade, because they
are the checks whose answers the decision may not need. A broken pairing withholds the downgrade and
skips the permission check. Every other check runs before `decide`, which therefore takes only a
team, a route and a spend figure, and performs no I/O.

## Native vs built

Configuring an existing capability was preferred to reimplementing it. Every native claim below was
read from the LiteLLM 1.101.0 source that the links cite.

| Native, configured and reused | Where |
|---|---|
| Worst-case cost reserved during authentication, before any callback runs | [`proxy/auth/user_api_key_auth.py:2667`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/user_api_key_auth.py#L2667) |
| The reservation increments the counter with Redis `INCRBYFLOAT` | [`proxy/proxy_server.py:3283`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L3283), [`caching/redis_cache.py:1195`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/caching/redis_cache.py#L1195) |
| Counters for Key, Team and User, only when `max_budget > 0` | [`proxy/spend_tracking/budget_reservation.py:417-474`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L417-L474) |
| An exhausted budget raises `BudgetExceededError` with `status_code = 429` at auth | [`proxy/spend_tracking/budget_reservation.py:161`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L161), [`exceptions.py:972`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/exceptions.py#L972) |
| The hard boundary. With `fail_closed_budget_enforcement` on, a worst case that does not fit the remaining headroom is refused whole at authentication. With it off, the hold is resized to the headroom instead | [`proxy/spend_tracking/budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141), [`budget_reservation.py:147`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L147), [`proxy/proxy_server.py:2429`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2429) |
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
| Versioned, cross-replica deduplication | Each claim carries team, policy epoch, window and transition. The epoch is derived from the team's policy, so a policy change re-arms its transitions |

## Claims and the policy epoch

A decision reads the team counter minus the request's own hold. The hold is a temporary
authorisation, so a request judged with it would push itself toward its own downgrade. Holds of
other in-flight requests stay in the figure, because each will produce a real charge.

Each transition is claimed with a Redis `SET NX` and a TTL of twice the window, so it is announced
once per window across every replica. The window is LiteLLM's next reset instant for the team's
`budget_duration`, from `get_budget_reset_time` with the gateway's `timezone` and
`budget_reset_time`, written in UTC.

| Transition | Claim key |
|---|---|
| Team tier | `brownout:tier:<team>:<epoch>:<window>:team:<tier>` |
| Route tier | `brownout:tier:<team>:<epoch>:<window>:route:<route>:<tier>` |
| Route degraded | `brownout:degraded:<team>:<epoch>:<window>:<route>` |
| Fallback rejected | `brownout:fallback_rejected:<team>:<epoch>:<window>:<route>:<rejection>` |
| Incident | `brownout:incident:<reason>:<subject>:<window>` |

The epoch is the first 12 hex characters of a SHA-256 over the team's parsed `envelope_usd`,
`budget_duration`, `warn_tiers` and routes. Key order, comments and writing `500` for `500.0` leave
it unchanged. An `unpriced_model` incident is keyed on the model group. A `model_not_a_group`
incident is keyed on the team, because a key holder could otherwise raise unbounded incidents by
inventing model names.

## Known limitations

Every known limit and its cause, and the work that remains, are in [Limits](limits.md).
