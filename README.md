<div align="center">

# LiteLLM Brownout

**Move a LiteLLM team's chosen routes to cheaper models as its budget fills**

Give a route a fallback and a percentage, and the gateway switches that route once the team's spend
passes it.

<p align="center">
  <a href="https://pypi.org/project/litellm-brownout/"><img src="https://img.shields.io/pypi/v/litellm-brownout?style=flat&labelColor=000000&color=000000" alt="PyPI version" /></a>
  <a href="https://github.com/amirimi/litellm-brownout/blob/main/LICENSE"><img src="https://img.shields.io/github/license/amirimi/litellm-brownout?style=flat&labelColor=000000&color=000000" alt="License" /></a>
</p>

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/amirimi/litellm-brownout/main/.github/assets/litellm-brownout.gif" alt="Team-a's weekly budget fills toward $500. The gateway log announces the 75% and 90% tiers, then chat-qa switches to its fallback with x-brownout headers while code-review keeps its primary.">
</p>

Take a team with $500 a week. Its Q&A route falls back to a smaller model at 90%, and its code review
route has no fallback.

| Team spend | Q&A route | Code review route | Signal |
|---|---|---|---|
| Below $375 | Primary | Primary | |
| $375 to $449.99 | Primary | Primary | 75% warning |
| $450 and above | Fallback | Primary | 90% warning and downgrade headers |
| Too little left for the next request | Refused | Refused | LiteLLM's 429 |

LiteLLM already reserves each request's cost, counts team spend across replicas and refuses requests
past the budget. Brownout adds four things.

- **Warnings at any percentage:** each team and route sets its own tiers, and each tier is logged
  once per budget window across all replicas. LiteLLM's own budget alerts fire at 85% and 95%.
- **Downgrades on the team's total:** a route switches to its fallback at the percentage you set.
  LiteLLM's [budget fallbacks][litellm-budget-fallbacks] switch only when a key runs out of a
  per-model budget.
- **Checked fallbacks:** brownout switches only to a fallback the gateway serves, that has a price,
  and that the caller's key and team may call. Otherwise the primary keeps serving.
- **Disclosure:** `x-brownout-*` response headers and LiteLLM's spend logs name the model that
  answered and why.

Downgrades cover chat, text completion, `/v1/messages`, `/v1/responses` and Google's
`generateContent`, so Claude Code and Codex sessions carry on with the fallback. Embeddings, images,
audio and other model calls keep their model and still get the warnings and the headers.

## Requirements

Brownout governs every model call on the gateway, so the whole gateway has to meet these. `init`
checks each one and prints the fix.

- **A team for every key:** every key that calls a model, an MCP tool, files, vector stores or
  containers belongs to a team in the manifest, because brownout reads the team's budget.
- **No key budgets:** a budget on a key is a second ceiling the manifest cannot see.
- **Reviewed call types only:** provider pass-through routes, realtime, batches, fine-tuning and
  search tools get a 503, because brownout refuses what it has not reviewed.
- **Fail-closed budgets and Redis:** `fail_closed_budget_enforcement` is on and `coordination_redis`
  is configured. LiteLLM keeps each team's spend counter in that Redis, and brownout records its
  announcements there.
- **LiteLLM 1.101.6 to 1.106, pinned by image tag:** brownout imports three private LiteLLM helpers,
  so upgrade only to a release the weekly `litellm matrix` workflow has passed.

If your gateway has no Redis, add one before running `init` and give the gateway the same
`REDIS_PASSWORD`. With Docker Compose the service looks like this, and its host for `init` is
`redis`. The append-only file and `noeviction` keep the counter through restarts and memory pressure.

<!-- adoption: redis-service -->
```yaml
services:
  redis:
    image: redis:7
    command: ["sh", "-c", "exec redis-server --requirepass \"$$REDIS_PASSWORD\" --appendonly yes --maxmemory-policy noeviction"]
    env_file:
      - .env
```

## Install

You run `init` against your gateway, fix what it finds, let it write two files, then add the package
to the gateway image.

**1. Run `init`.** The first line waits for you to paste the master key and shows no prompt. The
block then creates a read-only `brownout-init` user for `init`. Run it from a shell that exports no
other secret, such as a provider key.

<!-- adoption: prepare -->
```bash
read -rs LITELLM_MASTER_KEY && typeset +x LITELLM_MASTER_KEY
BROWNOUT_INIT_KEY=$(curl -sSf http://localhost:4000/user/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"user_id": "brownout-init", "user_role": "proxy_admin_viewer"}' | jq -er .key) \
  && export BROWNOUT_INIT_KEY
uvx --from litellm-brownout brownout init --gateway http://localhost:4000 --gateway-config config.yaml
```

`init` lists every request brownout would refuse, such as keys without a team or models without a
price, and prints the command or config lines that fix each one. Fix them and rerun the last line
until it exits `0`, then rerun it with `--write`. On OpenRouter every model needs a price, and
[one command][openrouter-prices] prints it.

**2. Check the files.** `--write` creates `brownout.yaml`, the manifest with one entry per team, and
`brownout.config.yaml`, your config plus the callback and the settings brownout needs. Check both,
then delete the `brownout-init` user, which deletes its key too.

<!-- adoption: check -->
```bash
uvx --from litellm-brownout brownout check --gateway-config brownout.config.yaml --manifest brownout.yaml
curl -sSf http://localhost:4000/user/delete \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"user_ids": ["brownout-init"]}'
```

**3. Add the package to the gateway image.** The LiteLLM image ships without `pip`, so the line
bootstraps it first.

<!-- adoption: install -->
```dockerfile
FROM docker.litellm.ai/berriai/litellm:v1.106.1
RUN python -m ensurepip --upgrade && python -m pip install --no-deps litellm-brownout==0.1.0
```

Point the gateway at `brownout.config.yaml`, set `BROWNOUT_MANIFEST` to the path of `brownout.yaml`
in the container, and restart it. On a gateway installed with pip, run the same `pip install` in its
environment. `--no-deps` leaves the gateway's LiteLLM as it is.

**4. Confirm.** Send one request with a team's key. The master key and the viewer key belong to no
team, so brownout refuses both.

<!-- adoption: confirm -->
```bash
curl -s -i http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer <A TEAM KEY>" -H "Content-Type: application/json" \
  -d '{"model": "<A MODEL>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20}' \
  | grep -io '^x-brownout-[a-z-]*: [^[:cntrl:]]*\|"reason": *"[a-z_]*"'
```

LiteLLM's default log level hides brownout's load message, so this header is the check. The
response carries `x-brownout-route-served` with the model that answered. A refused request prints
its reason instead, such as `"reason":"model_not_a_group"` for a name the gateway does not
configure.

## Quickstart

Give `chat-qa` a fallback that takes over at 90% of the team's budget.

<!-- adoption: fallback-manifest -->
```yaml
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

Add `chat-qa-small` to the team's model list in LiteLLM, or the primary keeps serving. Run
`brownout check` again and restart the gateway, which reads the manifest only at startup. For a
model added in the admin UI, `check` reads the gateway with a viewer key.

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

Once the team passes $450, every `chat-qa` response carries these headers.

```
x-brownout-route-served: chat-qa-small
x-brownout-budget-tier: 90
x-brownout-degraded-from: chat-qa
x-brownout-degradation-reason: degrade_tier_reached
```

Send the fallback the route's real requests before it carries real traffic, because it gets
everything the route sends, tools and images included.

To point Claude Code or Codex at a route, follow [their setup][agents]. A manifest can also require
each fallback to be cleared for its route's [data class][data-classes]. To watch a whole budget
drain without spending anything, run the [lab][lab].

## Limits

- **Only text generation downgrades:** embeddings, images, audio and Codex's `/v1/responses/compact`
  keep their model at any spend.
- **Requests name a model group:** a router alias, a wildcard group such as `openai/*` or a list of
  models gets a 503.
- **No router fallbacks:** a provider error on the chosen model reaches the caller. Failover between
  deployments of one model group still works.
- **Only key and team model lists are checked:** brownout never reads project, team member or
  customer model lists. If you use them, give a route a fallback only when every list that admits
  the primary also admits the fallback.
- **The reservation comes first:** LiteLLM reserves each request's worst case on the model the
  caller asked for. Set `max_tokens`, or a large request near the ceiling gets LiteLLM's 429 before
  the downgrade.
- **Redis is on the request path:** while LiteLLM cannot reach Redis it refuses every budgeted
  request, and it keeps refusing for 60 seconds after Redis returns.

The [full list][limits] gives each limit with its cause. If you run brownout on a real workload,
[open an issue][issues] with the model pair, budget window, threshold and LiteLLM version, and say
whether the fallback stayed useful.

## Documentation

The reference docs state every rule, assumption and failure mode exactly, for reviewers and for
agents reviewing brownout.

- [Configuration][configuration]: every setting, manifest key, rule and `init` option.
- [Enforcement][enforcement]: what brownout assumes, how it fails, and its headers and events.
- [Limits][limits]: every known limit and its cause.
- [Claude Code and Codex][agents]: the settings that point both tools at a route.
- [Design][design]: how the modules fit together.
- [Security][security]: what brownout reads and writes, and how to verify a release.
- [Contributing][contributing]: the tests, the supported LiteLLM releases and releasing.

## License

[MIT][license]

[litellm-budget-fallbacks]: https://docs.litellm.ai/docs/proxy/budget_fallbacks
[openrouter-prices]: https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md#openrouter-prices
[agents]: https://github.com/amirimi/litellm-brownout/blob/main/docs/claude-code-and-codex.md
[data-classes]: https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md#data-classes
[lab]: https://github.com/amirimi/litellm-brownout/blob/main/docs/lab.md
[limits]: https://github.com/amirimi/litellm-brownout/blob/main/docs/limits.md
[issues]: https://github.com/amirimi/litellm-brownout/issues
[configuration]: https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md
[enforcement]: https://github.com/amirimi/litellm-brownout/blob/main/docs/enforcement.md
[design]: https://github.com/amirimi/litellm-brownout/blob/main/docs/design.md
[security]: https://github.com/amirimi/litellm-brownout/blob/main/SECURITY.md
[contributing]: https://github.com/amirimi/litellm-brownout/blob/main/CONTRIBUTING.md
[license]: https://github.com/amirimi/litellm-brownout/blob/main/LICENSE
