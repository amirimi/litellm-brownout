# LiteLLM Brownout

<p align="center">
  <img src="https://raw.githubusercontent.com/amk9978/litellm-brownout/main/docs/assets/litellm-brownout.gif" width="540" alt="Team-a's weekly budget fills toward $500. The gateway log announces the 75% and 90% tiers, then chat-qa switches to its fallback chat-qa-degraded with x-brownout headers while code-review keeps its primary.">
</p>

Brownout moves chosen routes on a LiteLLM gateway to cheaper models as a team's budget fills, and
tells each caller which model answered.

A team has $500 a week. At $450 its Q&A route moves to a smaller model, and its code review route
keeps the primary until LiteLLM stops the team at $500.

| Team spend | Q&A route | Code review route | Signal |
|---|---|---|---|
| Below $375 | Primary | Primary | |
| $375 to $449.99 | Primary | Primary | 75% warning |
| $450 and above | Fallback | Primary | 90% warning and downgrade headers |
| Too little left for the next request | Refused | Refused | LiteLLM's 429 |

## What it adds

LiteLLM already reserves each request's worst-case cost, counts team spend across replicas in Redis
and refuses requests past the budget. Brownout adds four things on top. Text generation through
`/v1/chat/completions`, `/v1/completions`, `/v1/messages`, `/v1/responses` and Google's
`generateContent` gets all four. Embeddings, images, audio and the other model calls get the
warnings and the disclosure, and always keep the model they named.

- **Warnings at any percentage.** The team and each route declare their own tiers, and each tier is
  logged once per window across all replicas. LiteLLM's own budget alerts fire at 85% and 95% only.
- **A downgrade on the team's total.** A route names a fallback and the percentage at which to use
  it. LiteLLM's [budget fallbacks][litellm-budget-fallbacks] switch only when a key exhausts a
  per-model budget.
- **Checked fallbacks.** A fallback must be served, priced, cleared for the route's data class and
  callable by the caller's key and team. A fallback that fails a check leaves the primary serving.
- **Disclosure.** `x-brownout-*` response headers name the model group that served and the reason
  for a downgrade. The response body's `model` keeps the name the caller asked for, so a caller reads
  `x-brownout-route-served` to learn what answered. The same facts reach LiteLLM's spend logs and
  every logging integration the gateway runs, such as Langfuse or OpenTelemetry.

## Requirements

Brownout governs every model call on the gateway, so the whole gateway has to meet these.

- Every key that calls a model, an MCP tool, files, vector stores or containers belongs to a team
  in the manifest.
- No key carries a budget of its own.
- Calls use the call types brownout has reviewed. Provider pass-through routes, realtime, batches,
  fine-tuning and search tools get a 503 naming the call type.
- `fail_closed_budget_enforcement` is on, `coordination_redis` is configured and LiteLLM's budget
  alerts are off.
- LiteLLM runs a release from 1.101.6 to 1.104, pinned by image tag.

Brownout needs a Redis that LiteLLM can reach. LiteLLM keeps each team's spend counter there, and
brownout records each announcement there. If the gateway has no Redis, add one before running
`init`, and give the gateway the same `REDIS_PASSWORD`. With Docker Compose the service looks like
this, and its host for `init` is `redis`.

<!-- adoption: redis-service -->
```yaml
services:
  redis:
    image: redis:7
    command: ["sh", "-c", "exec redis-server --requirepass \"$$REDIS_PASSWORD\" --appendonly yes --maxmemory-policy noeviction"]
    env_file:
      - .env
```

The append-only file and the `noeviction` policy keep the counter and the announcements through a
restart or memory pressure.

## Install

`brownout init` reads your running gateway and reports what breaks the requirements. You resolve
its findings, let it write two files, install the package into the gateway image, and confirm with
one request.

### 1. Prepare

`init` reads the gateway with a read-only key. These commands, for bash or zsh, create one for a
new `proxy_admin_viewer` user called `brownout-init` and run `init`.

<!-- adoption: prepare -->
```bash
read -rs LITELLM_MASTER_KEY && typeset +x LITELLM_MASTER_KEY
BROWNOUT_INIT_KEY=$(curl -sSf http://localhost:4000/user/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"user_id": "brownout-init", "user_role": "proxy_admin_viewer"}' | jq -er .key) \
  && export BROWNOUT_INIT_KEY
uvx --from litellm-brownout brownout init --gateway http://localhost:4000 --gateway-config config.yaml
```

The first line waits, without a prompt, for you to paste the master key. It keeps the key off the
screen, out of shell history and unexported, so `uvx` and the packages it installs never receive it
from the shell. Run these from a shell that exports no other secret, such as a provider key. If
`curl` fails, the key stays unset and `init` refuses to start. A second run of the block fails at
`curl`, because the user already exists.

`init` lists every request brownout would refuse once enabled. Typical findings are keys without a
team, keys with a budget of their own, and teams or models without a budget or a price. Each finding
prints the command or the config lines that resolve it, with a placeholder for any value only you can
choose. Resolve them and rerun only the `init` line, which reuses `$BROWNOUT_INIT_KEY`, until it
exits `0`. Only then add `--write`, which refuses to overwrite an existing file.

`--write` creates two files and leaves your config as it was.

- `brownout.yaml` is the manifest. It has one entry per LiteLLM team with a budget, copies that
  budget and window, warns at 75% and 90%, and declares no fallback yet. Pass
  `--manifest yours.yaml` to extend a manifest you already have.
- `brownout.config.yaml` is your gateway config with comments kept. It adds the callback, turns on
  `fail_closed_budget_enforcement`, removes `budget_alerts` from `alert_types`, sets
  `budget_exceeded_status_code: 429` and derives `coordination_redis` from a Redis cache if you have
  one.

Check both files, then delete the `brownout-init` user, which deletes its key too. The same delete
recovers a key lost with a closed shell before you run the block again.

<!-- adoption: check -->
```bash
uvx --from litellm-brownout brownout check --gateway-config brownout.config.yaml --manifest brownout.yaml
curl -sSf http://localhost:4000/user/delete \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"user_ids": ["brownout-init"]}'
```

### 2. Install

Add one line to the image your gateway runs. The LiteLLM image ships without `pip`, so the line
bootstraps it first.

<!-- adoption: install -->
```dockerfile
FROM docker.litellm.ai/berriai/litellm:v1.104.2
RUN python -m ensurepip --upgrade && python -m pip install --no-deps litellm-brownout==0.1.0
```

On a gateway installed with pip, run `pip install --no-deps litellm-brownout==0.1.0` in its
environment. `--no-deps` keeps pip from replacing the gateway's LiteLLM.

Point the gateway at `brownout.config.yaml`, set `BROWNOUT_MANIFEST` to the path of `brownout.yaml`
inside the container, and restart it.

### 3. Confirm

LiteLLM's default log level hides brownout's load message, so a response header confirms it
instead. Send one chat request with a key of a team in the manifest.

<!-- adoption: confirm -->
```bash
curl -s -i http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer <A TEAM KEY>" -H "Content-Type: application/json" \
  -d '{"model": "<A MODEL>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20}' \
  | grep -io '^x-brownout-[a-z-]*: [^[:cntrl:]]*\|"reason": *"[a-z_]*"'
```

The response carries `x-brownout-route-served` with the model group that answered. A refused request
prints its reason instead, such as `"reason":"model_not_a_group"` for a name that is not a model
group the gateway configures. Brownout refuses model calls made with the master key or the viewer
key, because neither belongs to a team.

## Add a fallback

A route degrades only after its owner gives it a fallback. Adding one takes an edit to each file.

In `brownout.yaml`, list the data classes least restricted first and give the route its fallback.

<!-- adoption: fallback-manifest -->
```yaml
data_class_order: [internal, customer_pii]
teams:
  team-a:
    envelope_usd: 500.0
    budget_duration: "7d"
    warn_tiers: [75, 90]
    routes:
      chat-qa:
        fallback: chat-qa-small
        degrade_at: 90
```

In `brownout.config.yaml`, give every deployment of both model groups a class from that list. The
fallback's class must be at least as restricted as the route's.

<!-- adoption: fallback-config -->
```yaml
model_list:
  - model_name: chat-qa
    litellm_params:
      model: openai/gpt-4o
    model_info:
      data_class: internal
  - model_name: chat-qa-small
    litellm_params:
      model: openai/gpt-4o-mini
    model_info:
      data_class: internal
```

A model added in the admin UI lives in LiteLLM's database instead of a file, so it takes its class
through LiteLLM's model patch. This loop patches every deployment of `chat-qa`. Run it for the
fallback too.

<!-- adoption: fallback-ui -->
```bash
for id in $(curl -sSf http://localhost:4000/model/info -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    | jq -r '.data[] | select(.model_name == "chat-qa") | .model_info.id'); do
  curl -sSf -X PATCH "http://localhost:4000/model/$id/update" \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
    -d '{"model_info": {"data_class": "internal"}}'
done
```

The team's model list in LiteLLM must include the fallback, or the primary keeps serving. Run
`brownout check` again, then restart the gateway, which reads the manifest only at startup. `check`
reads models from files, so for a model added in the admin UI it needs `--gateway` and a read-only
key, created and deleted as in Prepare.

<!-- adoption: fallback-ui-check -->
```bash
BROWNOUT_INIT_KEY=$(curl -sSf http://localhost:4000/user/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"user_id": "brownout-init", "user_role": "proxy_admin_viewer"}' | jq -er .key) \
  && export BROWNOUT_INIT_KEY
uvx --from litellm-brownout brownout check --gateway http://localhost:4000 --gateway-config brownout.config.yaml --manifest brownout.yaml
curl -sSf http://localhost:4000/user/delete \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"user_ids": ["brownout-init"]}'
```

Send the fallback the route's real requests before it carries real traffic. After the downgrade it
receives everything the route sends, including tools, structured output, images and long prompts,
and brownout checks none of them. With `drop_params: true`, LiteLLM strips what the fallback cannot
take and still answers 200. Turn `drop_params` off to get an error instead.

LiteLLM reserves each request's worst-case cost before brownout runs, sized on the route the caller
asked for. A gpt-4o request without `max_tokens` reserves its whole output, about $0.16. On a $0.50
budget LiteLLM refuses such requests from 67%, before a `degrade_at` of 75% is reached. Set
`max_tokens`, or keep `degrade_at` below 100% minus the largest reservation's share of the budget.

## Use with Claude Code and Codex

Both tools reach the gateway with a team key and name model groups in place of their vendors' model
IDs. Point the tool at the gateway, name a route for every model it sends, and give it the context
window of the smaller model in that route's pairing.

Claude Code sends `/v1/messages`.

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_AUTH_TOKEN=<A TEAM KEY>
export ANTHROPIC_MODEL=<A ROUTE>
export ANTHROPIC_DEFAULT_HAIKU_MODEL=<A ROUTE>
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=<THE SMALLER CONTEXT WINDOW>
claude
```

`ANTHROPIC_DEFAULT_HAIKU_MODEL` covers the background requests Claude Code makes, such as session
titles. Without it those requests name an Anthropic model ID, which the gateway refuses unless it
has a model group of that name. Set `ANTHROPIC_DEFAULT_SONNET_MODEL` and
`ANTHROPIC_DEFAULT_OPUS_MODEL` too if users switch models with `/model`. Claude Code cannot tell a
gateway model's context window and assumes 200,000 tokens, so `CLAUDE_CODE_MAX_CONTEXT_TOKENS` sets
the real one. Use the smaller window of the route and its fallback, because a downgraded session
keeps its history.

Codex sends `/v1/responses`. Put this in `~/.codex/config.toml` and set `LITELLM_TEAM_KEY` to a team
key.

```toml
model = "<A ROUTE>"
model_provider = "litellm"
model_context_window = <THE SMALLER CONTEXT WINDOW>

[model_providers.litellm]
name = "LiteLLM gateway"
base_url = "http://localhost:4000/v1"
env_key = "LITELLM_TEAM_KEY"
wire_api = "responses"
```

Codex has no metadata for a gateway model name, warns about it and uses defaults.
`model_context_window` gives it the window to compact against.

Both tools ran through a governed gateway on OpenRouter models, below the downgrade tier and past
it, and finished a tool-using task on the primary and on the fallback.

## Try it locally

The lab runs LiteLLM, Postgres and Redis with brownout mounted and mock model responses, so it spends
nothing. It needs Docker Compose, Python 3.12+, [uv][uv], `curl` and `jq`. The first start downloads
about 2 GB.

```bash
git clone https://github.com/amk9978/litellm-brownout.git && cd litellm-brownout
cp .env.example .env
docker compose up -d --wait
set -a; source .env; set +a
KEY=$(uv run python tests/setup_lab.py | tail -1)
for i in $(seq 1 60); do
  curl -s -D - -o /dev/null localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"budget-lab-primary","messages":[{"role":"user","content":"hi"}],"max_tokens":40}' \
  | grep -i '^HTTP/\|^x-brownout'
done
```

The lab team has $1.00. Around request 38 it passes 75%, and responses switch to the fallback.

```
HTTP/1.1 200 OK
x-brownout-route-served: budget-lab-degraded
x-brownout-budget-tier: 75
x-brownout-degraded-from: budget-lab-primary
x-brownout-degradation-reason: degrade_tier_reached
```

Around request 57 LiteLLM refuses with a 429. `tests/setup_lab.py` deletes and recreates the
`budget-lab` team, so run it only against the lab. The [lab walkthrough][lab] has more experiments,
such as a refused downgrade and a broken manifest. `docker compose down` stops the lab.

## Limits

- Governed requests name concrete model groups. The plugin refuses a router alias, a routing
  group, a name a wildcard group such as `openai/*` serves, a deployment id and a list of models
  with a 503 `model_not_a_group`, and the manifest refuses a wildcard route.
- `check` and `init` read the config file you pass them. A gateway that loads its config from a
  bucket needs that file downloaded first.
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
- Router fallbacks never apply to governed requests, including the retry LiteLLM's local rate limiter
  makes. A provider error on the chosen model group reaches the caller, while failover between
  deployments of one group still works.
- Redis sits on the request path. While LiteLLM cannot write a reservation, it refuses every budgeted
  request. Once Redis is back, LiteLLM keeps refusing until its Redis circuit breaker resets, 60
  seconds by default. Run Redis with failover, persistence and the `noeviction` policy.
- Brownout imports three private LiteLLM helpers and refuses to start below 1.101.6. A release that
  renames a helper stops the gateway at startup, so pin the image tag and upgrade only to a release
  the weekly `litellm matrix` workflow has passed.
- A request that breaks a requirement gets a 503 naming the rule. The [enforcement
  reference][enforcement] lists every assumption, failure mode, header and event.

The [design notes][limitations] list the remaining limitations. If you run brownout on a real
workload, [open an issue][issues] with the model pair, budget window, threshold, LiteLLM version and
whether the fallback stayed useful.

## Documentation

- The [configuration reference][configuration] explains every setting, manifest key, rule and `init`
  option.
- The [enforcement reference][enforcement] covers the assumptions, failure modes, headers and events.
- The [design notes][design] show how the modules fit together.
- [SECURITY.md][security] says what brownout reads and writes and how to verify a release.
- [CONTRIBUTING.md][contributing] covers the tests, supported LiteLLM releases and releasing.

## License

[MIT][license]

[litellm-budget-fallbacks]: https://docs.litellm.ai/docs/proxy/budget_fallbacks
[uv]: https://docs.astral.sh/uv/
[lab]: https://github.com/amk9978/litellm-brownout/blob/main/docs/lab.md
[enforcement]: https://github.com/amk9978/litellm-brownout/blob/main/docs/enforcement.md
[limitations]: https://github.com/amk9978/litellm-brownout/blob/main/docs/design.md#known-limitations
[issues]: https://github.com/amk9978/litellm-brownout/issues
[configuration]: https://github.com/amk9978/litellm-brownout/blob/main/docs/configuration.md
[design]: https://github.com/amk9978/litellm-brownout/blob/main/docs/design.md
[security]: https://github.com/amk9978/litellm-brownout/blob/main/SECURITY.md
[contributing]: https://github.com/amk9978/litellm-brownout/blob/main/CONTRIBUTING.md
[license]: https://github.com/amk9978/litellm-brownout/blob/main/LICENSE
