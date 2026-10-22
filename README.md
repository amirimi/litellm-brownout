<div align="center">

# LiteLLM Brownout

**Move a LiteLLM team's chosen routes to cheaper models on budget tiers**

No budget runaway, no quick drain of a team budget. Give a route a fallback and a percentage,
and the gateway switches that route once the team's spend passes it.

<p align="center">
  <a href="https://pypi.org/project/litellm-brownout/"><img src="https://img.shields.io/pypi/v/litellm-brownout?style=flat&labelColor=000000&color=000000" alt="PyPI version" /></a>
  <a href="https://github.com/amirimi/litellm-brownout/blob/main/CONTRIBUTING.md#supported-litellm-releases"><img src="https://img.shields.io/badge/LiteLLM-1.101.6%20to%201.106-000000?style=flat&labelColor=000000" alt="LiteLLM 1.101.6 to 1.106" /></a>
  <a href="https://socket.dev/pypi/package/litellm-brownout"><img src="https://socket.dev/api/badge/pypi/package/litellm-brownout" alt="Socket score" /></a>
  <a href="https://github.com/amirimi/litellm-brownout/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/amirimi/litellm-brownout/ci.yml?branch=main&label=tests&style=flat&labelColor=000000" alt="Tests" /></a>
  <a href="https://github.com/amirimi/litellm-brownout/actions/workflows/litellm-matrix.yml"><img src="https://img.shields.io/github/actions/workflow/status/amirimi/litellm-brownout/litellm-matrix.yml?branch=main&label=LiteLLM%20matrix&style=flat&labelColor=000000" alt="LiteLLM matrix" /></a>
  <a href="https://github.com/amirimi/litellm-brownout/actions/workflows/adoption.yml"><img src="https://img.shields.io/github/actions/workflow/status/amirimi/litellm-brownout/adoption.yml?branch=main&label=install%20tested&style=flat&labelColor=000000" alt="Install tested" /></a>
</p>

<p align="center">
  <a href="https://github.com/amirimi/litellm-brownout#get-started"><img src="https://img.shields.io/badge/Docker%20Compose-000000?style=flat&logo=docker&logoColor=white" alt="Docker Compose" /></a>
  <a href="https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md#kubernetes-with-litellm-helm"><img src="https://img.shields.io/badge/Kubernetes%20with%20Helm-000000?style=flat&logo=kubernetes&logoColor=white" alt="Kubernetes with Helm" /></a>
  <a href="https://github.com/amirimi/litellm-brownout#other-ways-to-run-litellm"><img src="https://img.shields.io/badge/pip%20or%20uv-000000?style=flat&logo=python&logoColor=white" alt="pip or uv" /></a>
</p>

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/amirimi/litellm-brownout/main/.github/assets/litellm-brownout.gif" alt="Team-a's weekly budget fills toward $500. The gateway log announces the 75% and 90% tiers, then chat-qa switches to its fallback with x-brownout headers while code-review keeps its primary.">
</p>

## Example

Take a team with $500 a week. Its Q&A route falls back to a smaller model at 90%, and its code review
route has no fallback.

| Team spend | Q&A route | Code review route | Signal |
|---|---|---|---|
| Below $375 | Primary | Primary | |
| $375 to $449.99 | Primary | Primary | 75% warning |
| $450 and above | Fallback | Primary | 90% warning and downgrade headers |
| Too little left for the next request | Refused | Refused | LiteLLM's 429 |

LiteLLM already reserves each request's cost, counts team spend across replicas and refuses requests
past the budget. Brownout adds five things.

- **Warnings at any percentage:** each team and route sets its own tiers, logged once per budget
  window across replicas. LiteLLM's own budget alerts fire at 85% and 95%.
- **Downgrades on the team's total:** a route switches to its fallback at the percentage you set.
  LiteLLM's [budget fallbacks][litellm-budget-fallbacks] switch only when a key exhausts a per-model
  budget.
- **Coding agents included:** chat, text completion, Anthropic's `/v1/messages`, OpenAI's
  `/v1/responses` and Google's `generateContent` all downgrade, so a Claude Code or Codex session
  carries on with the fallback mid-task.
- **Checked fallbacks:** brownout switches only to a fallback the gateway serves and prices, and that
  the caller's key and team may call.
- **Disclosure:** `x-brownout-*` headers and the spend logs name the model that answered and why.

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/amirimi/litellm-brownout/main/.github/assets/how-it-works.svg" alt="A request passes LiteLLM's authentication and budget reservation. Brownout reads the team's spend from Redis, finds it past the route's tier and serves the fallback. The response returns with x-brownout headers. The manifest brownout.yaml sets the policy." width="900">
</p>

One manifest, `brownout.yaml`, holds every team's budget, tiers and fallbacks. It lives in your repo
next to the gateway config, so a policy change goes through code review, and `brownout check` runs
it in CI. LiteLLM keeps enforcing the budget, and brownout picks the model that serves each
request.

## Requirements

Brownout governs every model call on the gateway, so the gateway needs a few things in place. `init`
checks each one and prints the fix.

- Every key that calls a model belongs to a team, and that team is in the manifest.
- Budgets sit on teams, never on keys.
- Clients use LiteLLM's unified routes. Provider pass-through routes, realtime, batches, fine-tuning
  and search tools are refused.
- LiteLLM is 1.101.6 to 1.106, with its image pinned by digest.
- One LiteLLM process needs nothing more. [More than one](#more-than-one-litellm-process) needs
  Redis.

## Get started

Brownout is configuration as code. Its manifest, its gateway settings and its Compose override are
files in your repo, reviewed like any other change. One command writes all of them from your running
gateway, so run it from the folder with your compose file.

**1. Run `init` and fix what it lists.**

<!-- adoption: prepare -->
```bash
export BROWNOUT_INIT_KEY="{gateway key}"
uvx --from litellm-brownout brownout init --gateway http://localhost:4000 --gateway-config config.yaml
```

`{gateway key}` is any key that can read the gateway, the master key included, because `init` only
reads. Each finding prints its fix, and a fix that calls LiteLLM's API uses `$LITELLM_MASTER_KEY`.

**2. Write the files.**

<!-- adoption: write -->
```bash
uvx --from litellm-brownout brownout init --gateway http://localhost:4000 --gateway-config config.yaml --write
```

**3. Restart the gateway.**

<!-- adoption: restart -->
```bash
docker compose up -d
docker compose logs litellm | grep "brownout loaded"
```

What happened:

- `init` added brownout's settings to `config.yaml`, wrote the manifest `brownout.yaml`, and checked
  both.
- It copied the plugin into `plugins/` and wrote `docker-compose.override.yml`, which mounts it into
  your LiteLLM service. Your compose file and LiteLLM's image stay as they were.
- The log line names the plugin version and the teams it governs.

**Optional:** send one request with a team's key to see the headers. The master key belongs to no
team, so brownout refuses it.

<!-- adoption: confirm -->
```bash
curl -s -i http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer {team key}" -H "Content-Type: application/json" \
  -d '{"model": "{model}", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20}' \
  | grep -io '^x-brownout-[a-z-]*: [^[:cntrl:]]*\|"reason": *"[a-z_]*"'
```

OpenRouter models need a price, which [one command][openrouter-prices] prints.

### Other ways to run LiteLLM

The plugin needs three things in LiteLLM's container: `plugins/` mounted at `/app/plugins`, the
manifest at `/app/brownout.yaml` and `PYTHONPATH=/app/plugins`.

- **`docker run`:** with no compose file in the folder, `init --write` prints the flags to add.
- **Kubernetes with LiteLLM's `litellm-helm` chart:** add `--platform helm`, then follow
  [these steps][helm].
- **`pip` or `uv`:** install `litellm-brownout` into the proxy's environment, add `--platform pip`,
  and start the proxy from the folder that holds `brownout.yaml`. No mount is needed.
- **Anywhere else:** mount the same folder and file, and set the same variable.

To upgrade, run `uvx --from litellm-brownout=={version} brownout plugin --dir plugins` and restart the
gateway.

### More than one LiteLLM process

Several replicas or workers share their spend through Redis. Brownout uses the Redis LiteLLM already
has, whether it comes from `coordination_redis`, a Redis response cache, `router_settings` or
`REDIS_*` variables. A gateway without one adds this service and passes `--redis-host redis` to
`init --write`.

<!-- adoption: redis-service -->
```yaml
services:
  redis:
    image: redis:7
    command: ["sh", "-c", "exec redis-server --requirepass \"$$REDIS_PASSWORD\" --appendonly yes --maxmemory-policy noeviction"]
    env_file:
      - .env
```

## Your first fallback

Add a route under your team in `brownout.yaml`. Fill in each `{placeholder}`.

<!-- adoption: fallback-manifest -->
```yaml
teams:
  "{team}":
    routes:
      "{route}":
        fallback: "{fallback}"
        degrade_at: 90
```

Add `{fallback}` to the team's model list in LiteLLM, check the manifest, then restart the gateway
with `docker compose restart litellm`. `--gateway` lets the check see models added in the admin UI.

<!-- adoption: fallback-check -->
```bash
uvx --from litellm-brownout brownout check --gateway http://localhost:4000 --gateway-config config.yaml --manifest brownout.yaml
```

Once the team's spend passes 90%, every `{route}` response carries these headers.

```
x-brownout-route-served: {fallback}
x-brownout-budget-tier: 90
x-brownout-degraded-from: {route}
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
- **Without Redis, one process only:** several replicas or workers without Redis are unsupported.
  The gateway logs its mode on its first governed request.
- **With Redis, Redis is on the request path:** while LiteLLM cannot reach Redis it refuses every
  budgeted request.

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

If you find brownout useful, please star the repo. It helps other LiteLLM operators find it.

## License

[MIT][license]

[litellm-budget-fallbacks]: https://docs.litellm.ai/docs/proxy/budget_fallbacks
[openrouter-prices]: https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md#openrouter-prices
[agents]: https://github.com/amirimi/litellm-brownout/blob/main/docs/claude-code-and-codex.md
[lab]: https://github.com/amirimi/litellm-brownout/blob/main/docs/lab.md
[limits]: https://github.com/amirimi/litellm-brownout/blob/main/docs/limits.md
[issues]: https://github.com/amirimi/litellm-brownout/issues
[configuration]: https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md
[helm]: https://github.com/amirimi/litellm-brownout/blob/main/docs/configuration.md#kubernetes-with-litellm-helm
[enforcement]: https://github.com/amirimi/litellm-brownout/blob/main/docs/enforcement.md
[design]: https://github.com/amirimi/litellm-brownout/blob/main/docs/design.md
[security]: https://github.com/amirimi/litellm-brownout/blob/main/SECURITY.md
[contributing]: https://github.com/amirimi/litellm-brownout/blob/main/CONTRIBUTING.md
[license]: https://github.com/amirimi/litellm-brownout/blob/main/LICENSE
