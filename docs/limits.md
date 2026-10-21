# Limits

Brownout refuses some LiteLLM forms outright, and a running deployment inherits other behaviour
from how LiteLLM runs plugins. A request that breaks a requirement gets a 503 that names the rule.
The [enforcement reference](enforcement.md) lists every assumption, failure mode, header and event,
and its [assumptions table](enforcement.md#assumptions) lists the limits a rule enforces.

## Requests and setup

- Governed requests name concrete model groups. The plugin refuses a router alias, a routing
  group, a name a wildcard group such as `openai/*` serves, a deployment id and a list of models
  with a 503 `model_not_a_group`, and the manifest refuses a wildcard route.
- `check` and `init` read the config file you pass them. A gateway that loads its config from a
  bucket needs that file downloaded first.
- `check` prices models with the cost map its own LiteLLM loaded, which can differ from the
  gateway's copy. `check --gateway` reads the running gateway's prices instead.
- Brownout downgrades text generation only. Embeddings, Codex's `/v1/responses/compact`, images,
  audio, rerank, moderation, OCR and video keep the model they named at any spend, and their spend
  counts toward the team's budget.
- Brownout checks per-token prices on text generation and embeddings. LiteLLM prices images, audio,
  rerank, OCR and video per image, second, character, query or page, so brownout leaves those
  prices to LiteLLM, and a deployment LiteLLM cannot price records $0.
- A call type brownout has not reviewed gets a 503 `call_type_not_governed`. This covers provider
  pass-through routes such as `/anthropic/v1/messages`, realtime, batches, fine-tuning, search tools
  and any call type a later LiteLLM release adds. Use the unified routes instead.
- LiteLLM runs no plugin hook on its assistants, threads and runs endpoints, so brownout can neither
  govern nor refuse them.
- The team owner picks a fallback every client of its route can reach. LiteLLM sends
  `/v1/messages` to an OpenAI-style deployment as a Responses API call, so a fallback on a server
  without that API fails those calls with the provider's error.
- Redis sits on the request path. While LiteLLM cannot write a reservation, it refuses every budgeted
  request. Once Redis is back, LiteLLM keeps refusing until its Redis circuit breaker resets, 60
  seconds by default. Run Redis with failover, persistence and the `noeviction` policy.

## What a deployment inherits

| Limit | What it means for a deployment |
|---|---|
| Request compatibility between a route and its fallback goes unchecked | After the downgrade the fallback receives everything the route sends, including tools, structured output, images and long prompts, and its context capacity and modalities can differ. With `drop_params: true`, as in the lab config, LiteLLM strips what the fallback cannot take and still answers 200. Turn `drop_params` off to get an error instead. Send each fallback the route's real requests before it carries real traffic |
| The counter resets after the window does | LiteLLM's reset job zeroes the counter every 597 to 605 seconds by default, and later if the job is delayed. Until it runs, the previous window's spend can degrade requests and consume the new window's claims. A team that ends a window above a tier therefore gets false announcements at the next boundary, and none when it really crosses that tier later in the window. Closing this needs the team's persisted `budget_reset_at` |
| Router fallbacks never apply to governed model calls | The plugin empties every fallback list and sets `disable_fallbacks` on each call that names a model, so a gateway, key or team that relies on cross-group fallbacks for outage resilience loses them. The retry LiteLLM's local rate limiter makes is a router fallback too. A provider error on the chosen model group therefore reaches the caller. Failover between deployments of one route by `order` and cooldown still works |
| A fallback that errors fails the request | The caller receives the provider's error, and nothing retries the primary. When a large team crosses `degrade_at`, all its traffic for that route moves to the fallback at once, so the provider quota behind the fallback has to absorb the jump |
| Losing Redis keys repeats announcements | A flush, or an eviction policy such as `volatile-lru` that evicts keys with a TTL first, removes the plugin's claims, and every transition in the window is announced again |
| A LiteLLM release that renames a private helper stops startup | The bridge imports three private helpers when LiteLLM loads the callback, so a rename fails the import and the gateway does not start. The plugin also refuses to start below 1.101.6. Pin the image tag and upgrade only to a release the weekly `litellm matrix` workflow has passed |
| Routing can alternate near a threshold under concurrency | Each decision reads a counter that includes the worst-case holds of in-flight requests, which settle lower. Near `degrade_at`, primary and degraded responses can interleave until settled spend passes the threshold |
| Reset times that daylight saving skips or repeats are unsupported | A `budget_reset_time` that does not occur exactly once on a reset date, such as 02:30 or 01:30 in a US zone, makes LiteLLM's own schedule move early or backward on the transition day |
| Only the budget amount is re-checked per request | The authenticated object carries `team_max_budget` and no duration or reset instant ([`proxy/_types.py:2879`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/_types.py#L2879)), so a drifted window is caught where the team is rendered |
| Other owners' LiteLLM budgets are not modelled | A user, organisation, end-user or tag budget, or a stacked team `budget_limits` window, can refuse a request before any tier fires. The authenticated object exposes the user, organisation and end-user budgets per request, and they stay unchecked because refusing them would break gateways that use them for other governance. Team `budget_limits` and tag budgets are not exposed per request |
| Entitlement reads the key's and team's model lists alone | A fallback reachable only through an access group or a team alias reads as unselectable, so the primary is served. Project, team member and customer model lists are never read, because the authenticated caller carries only their ids and the lists live in LiteLLM's database. A downgrade therefore serves a fallback that one of those lists excludes. On a gateway that uses them, give a route a fallback only when every project, member and customer list that admits the primary also admits the fallback. Customer lists exist from LiteLLM 1.106, and project lists need LiteLLM's enterprise package |
| An agent key is never downgraded | LiteLLM can limit an agent key's models through its access groups, the caller that invoked it and, from 1.105, managed grants. Brownout checks a fallback against the key's and team's lists alone. A request from a key with an `agent_id` therefore keeps its primary, and the owner gets `brownout_fallback_rejected` with `agent_key`. The agent spends at the primary's price until LiteLLM's budget refusal |
| Data classes are compared as an ordering | The whole rule for two classes. Provider, region, retention, purpose and audit requirement can each be more restrictive without the combination being permitted, and a production deployment needs a matrix over those dimensions |
| Money is a `float` | It matches the counter LiteLLM increments with `INCRBYFLOAT`. Nothing here is the system of record for spend |
| The reservation is sized on the requested route, never the served one | Reservation runs at authentication and the rewrite runs after it, so a degraded request still needs the primary's worst case to fit the headroom. Admission therefore stops at the envelope minus the requested route's worst case, and a degrade threshold above that ratio never fires. A gpt-4o request without `max_tokens` reserves its whole output, about $0.16, so on a $0.50 budget LiteLLM refuses such requests from 67%, before a `degrade_at` of 75% is reached. Set `max_tokens`, or keep `degrade_at` below 100% minus the largest reservation's share of the budget |
| A request LiteLLM cannot estimate places no hold | `reserve_budget_for_request` returns before reserving when the worst-case estimate is `None` ([`budget_reservation.py:236-237`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L236-L237)). Rule `model-priced` refuses a chat request for a model LiteLLM cannot price. Non-chat call types pass through the plugin untouched, so an embeddings, audio or image route the estimate cannot price is admitted on LiteLLM's read alone |
| The request rules cost well under a millisecond | The pre-call hook reads each deployment's billed price through `Router.get_deployment_model_info` and checks the name against `Router.model_list`, at about 0.01 ms on the lab config, and the fallback group too when a downgrade is wanted |

## Work that remains

- A request LiteLLM refuses at its budget ceiling carries no `x-brownout-*` header. LiteLLM refuses
  it during authentication, before the plugin's pre-call hook writes the request's facts, so the
  signal for that request lives in the body of LiteLLM's budget refusal. A response middleware seam
  would close this.
- The end-to-end test is serial, so concurrent admission near a threshold is argued in the unit
  suite and has yet to be driven through a live gateway.
