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

- **Warnings at any percentage:** each team and route sets its own tiers, logged once per budget
  window across replicas. LiteLLM's own budget alerts fire at 85% and 95%.
- **Downgrades on the team's total:** a route switches to its fallback at the percentage you set.
  LiteLLM's [budget fallbacks][litellm-budget-fallbacks] switch only when a key exhausts a per-model
  budget.
- **Checked fallbacks:** brownout switches only to a fallback the gateway serves and prices, and that
  the caller's key and team may call.
- **Disclosure:** `x-brownout-*` headers and the spend logs name the model that answered and why.

Downgrades cover chat, text completion, `/v1/messages`, `/v1/responses` and `generateContent`, so
Claude Code and Codex sessions carry on with the fallback.

## Requirements

Brownout governs every model call on the gateway. `init` checks each requirement and prints the fix.

- **A team for every key:** brownout reads the team's budget, so every key belongs to a manifest
  team.
- **No key budgets:** a key budget is a second ceiling the manifest cannot see.
- **Unified routes only:** provider pass-through routes, realtime, batches, fine-tuning and search
  tools get a 503.
- **Fail-closed budgets and Redis:** `fail_closed_budget_enforcement` is on and `coordination_redis`
  is set.
- **LiteLLM 1.101.6 to 1.106:** pin the image tag.

A gateway without Redis needs one that shares its `REDIS_PASSWORD`, such as this service.

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

**1. Run `init`.** Paste the master key on the first line, which shows no prompt. The block creates
a read-only `brownout-init` user for `init`.

<!-- adoption: prepare -->
```bash
read -rs LITELLM_MASTER_KEY && typeset +x LITELLM_MASTER_KEY
BROWNOUT_INIT_KEY=$(curl -sSf http://localhost:4000/user/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"user_id": "brownout-init", "user_role": "proxy_admin_viewer"}' | jq -er .key) \
  && export BROWNOUT_INIT_KEY
uvx --from litellm-brownout brownout init --gateway http://localhost:4000 --gateway-config config.yaml
```

`init` lists every request brownout would refuse with the fix for each. Rerun it until it exits `0`,
then once more with `--write`. OpenRouter models need a price, which [one command][openrouter-prices]
prints.

**2. Check the files.** `--write` creates `brownout.yaml`, the manifest, and `brownout.config.yaml`,
your config with brownout's settings. Check both, then delete the `brownout-init` user.

<!-- adoption: check -->
```bash
uvx --from litellm-brownout brownout check --gateway-config brownout.config.yaml --manifest brownout.yaml
curl -sSf http://localhost:4000/user/delete \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"user_ids": ["brownout-init"]}'
```

**3. Add the package to the gateway image.**

<!-- adoption: install -->
```dockerfile
FROM docker.litellm.ai/berriai/litellm:v1.106.1
RUN python -m ensurepip --upgrade && python -m pip install --no-deps litellm-brownout==0.1.0
```

Point the gateway at `brownout.config.yaml`, set `BROWNOUT_MANIFEST` to the path of `brownout.yaml`
in the container, and restart it.

**4. Confirm.** Send one request with a team's key. The master key belongs to no team, so brownout
refuses it.

<!-- adoption: confirm -->
```bash
curl -s -i http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer <A TEAM KEY>" -H "Content-Type: application/json" \
  -d '{"model": "<A MODEL>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20}' \
  | grep -io '^x-brownout-[a-z-]*: [^[:cntrl:]]*\|"reason": *"[a-z_]*"'
```

`x-brownout-route-served` names the model that answered. A refused request prints its reason, such
as `"reason":"model_not_a_group"`.

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

Add `chat-qa-small` to the team's model list in LiteLLM, run `brownout check` again and restart the
gateway. For models added in the admin UI, `check` reads the gateway with a viewer key.

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

Past $450, every `chat-qa` response carries these headers.

```
x-brownout-route-served: chat-qa-small
x-brownout-budget-tier: 90
x-brownout-degraded-from: chat-qa
x-brownout-degradation-reason: degrade_tier_reached
```

The fallback receives everything the route sends, tools and images included, so try it on the
route's real requests first. To point Claude Code or Codex at a route, follow [their
setup][agents]. The [lab][lab] drains a budget without spending anything.

## Limits

- **Only text generation downgrades:** embeddings, images, audio and Codex's `/v1/responses/compact`
  keep their model at any spend.
- **Requests name a model group:** a router alias, a wildcard group such as `openai/*` or a list of
  models gets a 503.
- **No router fallbacks:** a provider error on the chosen model reaches the caller. Failover between
  deployments of one model group still works.
- **The reservation comes first:** LiteLLM reserves each request's worst case on the model the
  caller asked for. Set `max_tokens`, or a large request near the ceiling gets LiteLLM's 429 before
  the downgrade.
- **Redis is on the request path:** while LiteLLM cannot reach Redis it refuses every budgeted
  request.

The [full list][limits] covers the rest. If you run brownout on a real workload, [open an
issue][issues] with the model pair, budget window, threshold and LiteLLM version, and say whether
the fallback stayed useful.

## Documentation

- [Configuration][configuration]: settings, manifest keys, rules and `init` options.
- [Enforcement][enforcement]: refusals, headers and events.
- [Limits][limits]: every known limit.
- [Claude Code and Codex][agents]: pointing both tools at a route.
- [Design][design]: how the modules fit together.
- [Security][security]: what brownout reads and writes, and how to verify a release.
- [Contributing][contributing]: the tests, the supported LiteLLM releases and releasing.

## License

[MIT][license]

[litellm-budget-fallbacks]: https://docs.litellm.ai/docs/proxy/budget_fallbacks
[openrouter-prices]: https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md#openrouter-prices
[agents]: https://github.com/amirimi/litellm-brownout/blob/main/docs/claude-code-and-codex.md
[lab]: https://github.com/amirimi/litellm-brownout/blob/main/docs/lab.md
[limits]: https://github.com/amirimi/litellm-brownout/blob/main/docs/limits.md
[issues]: https://github.com/amirimi/litellm-brownout/issues
[configuration]: https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md
[enforcement]: https://github.com/amirimi/litellm-brownout/blob/main/docs/enforcement.md
[design]: https://github.com/amirimi/litellm-brownout/blob/main/docs/design.md
[security]: https://github.com/amirimi/litellm-brownout/blob/main/SECURITY.md
[contributing]: https://github.com/amirimi/litellm-brownout/blob/main/CONTRIBUTING.md
[license]: https://github.com/amirimi/litellm-brownout/blob/main/LICENSE
