# Limits

Each limit says what it means for your gateway. Refusals, headers and events are in the
[enforcement reference](enforcement.md).

## Requests and setup

- Requests name concrete model groups. A router alias, a routing group, a name a wildcard group
  such as `openai/*` serves, a deployment id or a list of models gets a 503 `model_not_a_group`.
- `check` and `init` read the config file you pass them. A gateway that loads its config from a
  bucket needs that file downloaded first.
- `check` prices models with its own LiteLLM's cost map.
  `check --gateway` reads the running gateway's prices instead.
- Only text generation downgrades. Embeddings, Codex's `/v1/responses/compact`, images, audio,
  rerank, moderation, OCR and video keep their model, and their spend counts toward the budget.
- Brownout checks per-token prices on text generation and embeddings only,
  and a deployment LiteLLM cannot price records $0.
- Provider pass-through routes such as `/anthropic/v1/messages`, realtime, batches, fine-tuning and
  search tools get a 503 `call_type_not_governed`. Use the unified routes.
- LiteLLM runs no plugin hook on its assistants, threads and runs endpoints, so brownout can neither
  govern nor refuse them.
- A fallback serves every endpoint its route's callers use. LiteLLM sends `/v1/messages` to an
  OpenAI-style deployment as a Responses API call, so a fallback on a server
  without that API fails those calls with the provider's error.

## Running

- **Without Redis, one LiteLLM process only:** several replicas or workers without a Redis that
  LiteLLM uses are unsupported, because each process would count only its own spend. Brownout
  cannot count processes, so the gateway logs its mode on its first governed request. A restart
  without Redis repeats the window's announcements once, and every governed admit reads the spend
  recorded in the database, which LiteLLM caches for 5 seconds.
- **With Redis, Redis is on the request path:** while LiteLLM cannot reach Redis, and for 60 seconds
  after it returns, LiteLLM refuses every budgeted request. Run Redis with failover, persistence
  and `noeviction`, because a flush or an eviction repeats the window's announcements.
- **A LiteLLM upgrade can stop startup:** brownout imports private LiteLLM helpers, so upgrade only
  to a release the `litellm matrix` workflow has passed.
- **The fallback gets everything the route sends:** tools, images and long prompts included. With
  `drop_params: true` LiteLLM strips what the fallback cannot take. Try each fallback on the
  route's real requests.
- **No router fallbacks on governed calls:** a provider error on the chosen model reaches the
  caller, and nothing retries the primary. Failover between deployments of one model group still
  works. A large team moves a route's whole traffic to the fallback at once, so the fallback's
  provider quota has to absorb the jump.
- **The reservation is sized on the requested route:** a degraded request still needs the primary's
  worst case to fit. A gpt-4o request without `max_tokens` reserves about $0.16, so on a $0.50
  budget LiteLLM refuses it from 67%, before a `degrade_at` of 75%. Set `max_tokens`, or keep
  `degrade_at` below 100% minus the largest request's share of the budget.
- **Tiers can fire early under concurrency:** the figure includes in-flight holds, so near
  `degrade_at` primary and fallback responses can interleave.
- **A new window can start on the old spend:** LiteLLM's reset job runs about every ten minutes, so a
  team that ended a window above a tier can get that tier's announcement again at the boundary.
- **Daylight saving:** a `budget_reset_time` that a transition skips or repeats, such as 02:30 in a
  US zone, moves LiteLLM's own schedule on that day.
- **A drifted `budget_duration` is caught by `init`:** a request carries only the budget amount.
- **Other LiteLLM budgets can refuse first:** a user, organisation, end-user or tag budget, or a
  stacked team `budget_limits` window, can refuse a request before any tier fires.
- **Only the key's and team's model lists are checked:** a fallback reachable only through an
  access group or a team alias keeps the primary.
  Project, team member and customer model lists are never read, so a downgrade can serve a fallback
  one of them excludes.
- **An agent key is never downgraded:** LiteLLM limits an agent's models in ways the key and team
  lists do not show, so the request keeps its primary and the owner gets
  `brownout_fallback_rejected` with `agent_key`.
- **Data classes are an ordering:** `data_class_order` compares two classes and nothing else, such
  as region or retention.
- **Some model calls place no hold:** rule `model-priced` refuses a text or embedding model without
  a per-token price. An image, audio or video model LiteLLM cannot estimate is served without one.
