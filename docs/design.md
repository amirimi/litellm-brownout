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
| after stage 2 | the request that exhausts the budget never reaches it, so degradation fires below 100% |
| before stage 5 | it cannot know which deployment will serve, so price is a property of the route |

## Native vs built

Configuring an existing capability was preferred to reimplementing it. Every native claim below was
read from the pinned source in `.venv/lib/python3.12/site-packages/litellm`.

| Native, configured and reused | Where |
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
| Tier events at 75%, 90% and any route-declared threshold | The native alerter is hardcoded to 85% and 95% ([`integrations/SlackAlerting/slack_alerting.py:648-653`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/integrations/SlackAlerting/slack_alerting.py#L648-L653)), and no `alerting` key is declared. A team `soft_budget` is one absolute-dollar threshold. It compares against the recorded spend on the team row, a different figure from the counter, only alerts, and emails `metadata.soft_budget_alerting_emails` ([`proxy/auth/auth_checks.py:5332`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/auth_checks.py#L5332), [`proxy/_types.py:1805`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/_types.py#L1805)) |
| Route-declared degradation on a team total | `budget_fallbacks` fires on per-model `model_max_budget`, never on a team total ([`proxy/hooks/model_max_budget_limiter.py:300`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/hooks/model_max_budget_limiter.py#L300)) |
| Validated fallback selection | Nothing native checks that a target is priced, cleared for the primary's data class, and callable by the caller's key and team |
| A catalog that raises on an unknown model | LiteLLM returns `None` for an unpriced model and skips reservation entirely ([`proxy/spend_tracking/budget_reservation.py:236`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L236)) |
| Caller disclosure | No native header carries the served route or the route it degraded from |
| Guardrail re-merge after a rewrite | The merge runs at stage 3 keyed on the requested model, so a stage 4 rewrite would drop the target's guardrails |
| Versioned, cross-replica deduplication | Each claim carries team, policy epoch, window and threshold, so an approved policy change re-arms its transitions |

## Known limitations

Two pieces of work remain.

- A request blocked at the native ceiling carries no platform header. LiteLLM wraps the
  response-headers hook in a try/except that swallows, and runs it on success only, so the signal
  for that request lives in the 429 body. A response middleware seam would close this.
- The end-to-end test is serial, so concurrent admission near a threshold is argued in the unit
  suite and has yet to be driven through a live gateway.

A deployment inherits six limits as the plugin stands.

| Limit | What it means for a deployment |
|---|---|
| Entitlement reads the direct key and team model lists | A target reachable only through an access group, a team alias, or project or member scope reads as unselectable, so the primary is served. Those scopes need LiteLLM's full resolved-model path |
| Only the budget amount is re-checked per request | The authenticated object carries `team_max_budget` and no duration or reset instant ([`proxy/_types.py:2879`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/_types.py#L2879)), so a drifted window is caught where the team is rendered. The manifest parses `budget_duration` at load, because one derivation raises on a bad value and the other quietly returns the next midnight |
| Data classes are compared as an ordering | The whole rule for two classes. Provider, region, retention, purpose and audit requirement can each be more restrictive without the combination being permitted, and a production deployment needs a matrix over those dimensions |
| Money is a `float` | It matches the counter LiteLLM increments with `INCRBYFLOAT`. Nothing here is the system of record for spend |
| The reservation is sized on the requested route, never the served one | Reservation runs at authentication and the rewrite runs after it, so a degraded request still needs the primary's worst case to fit the headroom. With `fail_closed_budget_enforcement` on, admission therefore stops at the envelope minus the requested route's worst case, and a degrade tier above that ratio never fires. Keep `max_tokens` small relative to the envelope, as the lab does |
| A request LiteLLM cannot estimate places no hold | `reserve_budget_for_request` returns before reserving when the worst-case estimate is `None` ([`budget_reservation.py:236-237`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L236-L237)). That is a group the router and the cost map cannot price, an input the token counter cannot count on a model with no `max_input_tokens`, or an image route priced per pixel or by size ([`budget_reservation.py:1222-1228`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L1222-L1228)). The fail-closed flag is consulted only after the estimate, so it changes nothing on this path. Admission falls back to `_team_max_budget_check` ([`auth_checks.py:5242`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/auth/auth_checks.py#L5242)), a plain read of a counter the response updates afterwards ([`proxy_server.py:2713`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2713)), so concurrent requests each pass on the same figure. The catalog proves the plugin knows a price, and LiteLLM estimates from the deployment's own price fields, so `tests/test_config_contract.py` keeps the two equal for every deployment in the lab config. Non-chat call types pass through the plugin untouched, so an embeddings, audio or image route the estimate cannot price is admitted on the read alone |
