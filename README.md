<div align="center">

# LiteLLM Brownout

**Switch selected routes to cheaper models before the team budget runs out**

A team has $500 for the week, and at $450 its routine Q&A moves to a smaller model while code review keeps its primary.

</div>

## Install

The plugin is a Python package that you mount into the stock LiteLLM 1.101.0 image. Clone the
repository to get the package, the example config and a lab stack. The lab needs Docker Compose,
Python 3.12+, [uv](https://docs.astral.sh/uv/) and `curl`.

```bash
git clone https://github.com/amk9978/litellm-brownout.git
cd litellm-brownout
cp .env.example .env
```

## Quickstart

The lab runs LiteLLM with the plugin mounted, alongside Postgres, Redis and a small event sink.
Those are real processes, and only the model responses and token prices are synthetic, so a run
spends no provider money. The lab team has a $1.00 budget, warning tiers at 75% and 90%, and a
primary route that downgrades at 75%.

```bash
docker compose up -d --wait
set -a; source .env; set +a
KEY=$(uv run python lab/setup_lab.py | tail -1)
for i in $(seq 1 60); do
  curl -s -D - -o /dev/null localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"budget-lab-primary","messages":[{"role":"user","content":"hi"}],"max_tokens":40}' \
  | grep -i '^HTTP/\|^x-platform'
done
```

`lab/setup_lab.py` creates a fresh lab team and prints its key, which takes about 11 seconds.
Early requests are served by `budget-lab-primary`. Around request 37 the team passes 75% and
responses switch to the fallback model.

```
HTTP/1.1 200 OK
x-platform-route-served: budget-lab-degraded
x-platform-budget-tier: 75
x-platform-degraded-from: budget-lab-primary
x-platform-degradation-reason: degrade_tier_reached
```

Around request 57 LiteLLM answers with its own 429, because the next request no longer fits the
budget. Run `curl -s localhost:8088/events | jq` to see the events the plugin raised on the way.

To repeat the run, rerun `lab/setup_lab.py` and the loop. The script deletes and recreates the
`budget-lab` team and clears its counters and event claims, so point it only at the lab gateway.
`docker compose down` stops the stack. The credentials in `.env.example` are for local development
only. The [lab walkthrough](docs/lab.md) covers more experiments, such as a refused downgrade and a
broken manifest.

## How it works

A team's spend passes up to three thresholds. Each warning tier raises one event. At a route's
`degrade_at` percentage, requests for that route are served by its approved fallback model, and the
response headers tell the caller. When the next request's worst-case cost no longer fits the
budget, LiteLLM refuses it with a 429. The $500 team above warns at 75% and 90%, and its Q&A route
degrades at 90%.

| Team spend | Q&A route | Code review route | Signal |
|---|---|---|---|
| Below $375 | Primary | Primary | |
| $375 to $449.99 | Primary | Primary | 75% warning |
| $450 and above | Fallback, when the caller may use it | Primary | 90% warning and downgrade headers |
| Too little headroom for the requested route's worst case | Refused | Refused | LiteLLM's 429 |

Spend here is LiteLLM's team counter, minus the current request's own reservation. The counter
includes other requests' outstanding reservations, so it runs ahead of settled spend. The plugin
decides on every request and keeps no downgrade state between requests.

LiteLLM 1.101.0 already reserves each request's worst-case cost, counts spend across replicas in
Redis and enforces the hard limit. The plugin adds four things its configuration cannot express.

- **Tier events at any threshold:** one event per tier, team and budget window, deduplicated across replicas. LiteLLM's built-in alerter is fixed at 85% and 95%.
- **Downgrade on the team total:** a route declares a fallback and the percentage at which to use it. LiteLLM's [budget fallbacks](https://docs.litellm.ai/docs/proxy/budget_fallbacks) switch only when a key exhausts a per-model budget.
- **Validated fallbacks:** a fallback must be priced and cleared for the primary's data class before the gateway starts. Each request also checks that the caller's key and team may call it.
- **Caller disclosure:** `x-platform-*` response headers name the route that served, the tier reached and the reason for a downgrade.

`src/budget_policy/policy.py` makes every decision in one pure function. `adapter.py` connects it
to LiteLLM's pre-call and post-call hooks.

## Add it to your gateway

Four changes wire the plugin into your own gateway. Mount the package into the LiteLLM container,
point two environment variables at your manifest and price catalog, name the handler in the
gateway config, then create each team in LiteLLM to match the manifest.

The callback governs every chat-completion call on the gateway, and a key whose team is missing
from the manifest gets a 503. On a shared gateway, add every team that sends chat traffic to the
manifest before you enable the callback.

The first two changes go in the LiteLLM service of your Compose file.

```yaml
services:
  litellm:
    image: docker.litellm.ai/berriai/litellm:v1.101.0
    environment:
      PYTHONPATH: /app/plugins
      BUDGET_POLICY_PRICES: /app/config/prices.yaml
      BUDGET_POLICY_MANIFEST: /app/config/manifest.yaml
    volumes:
      - ./src/budget_policy:/app/plugins/budget_policy:ro
      - ./config:/app/config:ro
    command: ["--config", "/app/config/config.yaml", "--port", "4000"]
```

The gateway config names the handler and turns on the Redis counter and the strict budget boundary.

```yaml
general_settings:
  fail_closed_budget_enforcement: true
  coordination_redis:
    host: os.environ/REDIS_HOST
    port: 6379
    password: os.environ/REDIS_PASSWORD

litellm_settings:
  callbacks: ["budget_policy.adapter.handler"]
```

Each deployment in `model_list` also carries its per-token prices and a `model_info.data_class`,
both equal to the price catalog. Leave `general_settings.alerting`, `router_settings.fallbacks` and
`disable_budget_reservation` out of the gateway config. Each LiteLLM team needs the manifest's
budget and duration and a model list that includes the fallback, and its keys carry no budget of
their own. The [configuration reference](docs/configuration.md) explains every setting and gives
the `curl` calls that create a team and key.

## Configuration

The manifest holds each team's policy, keyed by the LiteLLM `team_id`.

```yaml
version: "v1"

teams:
  team-a:
    policy_epoch: "team-a-v1"
    envelope_usd: 500.0
    budget_duration: "7d"
    warn_tiers: [75, 90]
    on_counter_unavailable: block
    routes:
      chat-qa:
        fallback: chat-qa-degraded
        degrade_at: 90
      code-review: {}
```

- **`envelope_usd` and `budget_duration`:** the budget the owner approved and its window. Both must equal the LiteLLM team's settings.
- **`warn_tiers`:** percentages of the budget. Each one raises a single event per window.
- **`routes`:** each model group's fallback and the percentage at which to use it. A route with an empty policy or no entry keeps its primary model and still counts against the budget.
- **`policy_epoch`:** a new value re-arms every tier inside the current window, for use after an approved policy change.
- **`on_counter_unavailable`:** the response when the spend counter cannot be read, one of `block`, `degrade` or `admit`.

The handler reads the manifest and the price catalog once at startup, so a change needs a gateway
restart. Unknown manifest keys are ignored, and a typo such as `degrade_At` leaves degradation off
without an error. The price catalog in `config/prices.yaml` gives every model group its per-token
prices and a data class. A request for a model the catalog does not price gets a 503.

## Notes

- **Version pin:** the plugin imports three private LiteLLM helpers and is pinned to 1.101.0. [CONTRIBUTING.md](CONTRIBUTING.md) lists the checks to run before moving to a newer image.
- **Test the fallback first:** the plugin checks price, data-class label and caller access. Context size, tools, structured output and modalities stay unchecked, so try the fallback on your workload before real traffic. Pick a cheaper fallback, because a dearer one can overshoot the budget.
- **Failures refuse the request:** an unpriced model, an unknown team or a budget that drifted from the manifest returns a 503 and raises a platform incident. The [enforcement reference](docs/enforcement.md) lists every failure mode, header, event and metric.
- **LiteLLM owns the hard limit:** `fail_closed_budget_enforcement` decides whether a window may close one request over budget. The trade-off is set out under [the envelope boundary](docs/enforcement.md#the-envelope-boundary).
- **Known limitations:** fallback entitlement reads only the direct key and team model lists, and reset windows other than `7d` need care. The [design notes](docs/design.md#known-limitations) list the rest.
- **Field reports:** trying it on a real workload? [Open an issue](https://github.com/amk9978/litellm-brownout/issues) with the model pair, budget window, threshold and whether the fallback stayed useful. Leave keys and private prompts out.

## License

[MIT](LICENSE)
