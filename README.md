<div align="center">

# LiteLLM Brownout

**Tiered budget warnings and graceful model downgrade for teams on a LiteLLM gateway**

Give each team a budget, warning tiers and an approved cheaper model, and the gateway steps down to that model before the budget runs out.

</div>

## Install

The plugin is a Python package that you mount into the stock LiteLLM 1.101.0 image. Clone the
repository to get the package, the example config and a lab stack. The lab needs Docker Compose
and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/amk9978/litellm-brownout.git
cd litellm-brownout
cp .env.example .env
```

## Quickstart

The lab runs LiteLLM with the plugin mounted, alongside Postgres, Redis and a small event sink.
Every route answers with a mock response, so a run spends no provider money. The lab team has a
$1.00 budget, warning tiers at 75% and 90%, and a primary route that downgrades at 75%.

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
The [lab walkthrough](docs/lab.md) covers more experiments, such as a refused downgrade and a
broken manifest.

## How it works

A team's spend passes three thresholds on its way to the budget. At each warning tier the plugin
raises one event. At a route's `degrade_at` percentage, requests for that route are served by its
approved fallback model, and the response headers tell the caller. At 100% LiteLLM refuses the
request with a 429.

LiteLLM 1.101.0 already reserves each request's worst-case cost, counts spend across replicas in
Redis and enforces the hard limit. The plugin adds four things its configuration cannot express.

- **Tier events at any threshold:** one event per tier, team and budget window, deduplicated across replicas. LiteLLM's built-in alerter is fixed at 85% and 95%.
- **Downgrade on the team total:** a route declares a fallback and the percentage at which to use it. LiteLLM's `budget_fallbacks` watches per-model budgets only.
- **Validated fallbacks:** a fallback must be priced and cleared for the primary's data class before the gateway starts. Each request also checks that the caller's key and team may call it.
- **Caller disclosure:** `x-platform-*` response headers name the route that served, the tier reached and the reason for a downgrade.

`src/budget_policy/policy.py` makes every decision in one pure function. `adapter.py` connects it
to LiteLLM's pre-call and post-call hooks. The [design notes](docs/design.md) map each native
LiteLLM capability to its source line.

## Add it to your gateway

Four changes wire the plugin into your own gateway. Mount the package into the LiteLLM container,
point two environment variables at your manifest and price catalog, name the handler in the
gateway config, then create each team in LiteLLM with a `max_budget` equal to its manifest budget.

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
  master_key: os.environ/LITELLM_MASTER_KEY
  fail_closed_budget_enforcement: true
  coordination_redis:
    host: os.environ/REDIS_HOST
    port: 6379
    password: os.environ/REDIS_PASSWORD

litellm_settings:
  callbacks: ["budget_policy.adapter.handler"]
```

Each deployment in `model_list` also carries its per-token prices and a `model_info.data_class`,
both equal to the price catalog. `lab/setup_lab.py` shows how to create a matching team and key.
The [configuration reference](docs/configuration.md) covers every setting, including three keys
that must stay out of the gateway config.

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
      route-chat:
        fallback: route-chat-degraded
        degrade_at: 90
```

- **`envelope_usd` and `budget_duration`:** the budget the owner approved and its window. Both must equal the LiteLLM team's settings.
- **`warn_tiers`:** percentages of the budget. Each one raises a single event per window.
- **`routes`:** each model group's fallback and the percentage at which to use it. An unlisted route still counts against the budget and never downgrades.
- **`policy_epoch`:** a new value re-arms every tier inside the current window, for use after an approved policy change.
- **`on_counter_unavailable`:** the response when the spend counter cannot be read, one of `block`, `degrade` or `admit`.

The price catalog in `config/prices.yaml` gives every model group its per-token prices and a data
class. A request for a model the catalog does not price gets a 503.

## Notes

- **Version pin:** the plugin imports three private LiteLLM helpers and is pinned to 1.101.0. [CONTRIBUTING.md](CONTRIBUTING.md) lists the checks to run before moving to a newer image.
- **Failures refuse the request:** an unpriced model, an unknown team or a budget that drifted from the manifest returns a 503 and raises a platform incident. The [enforcement reference](docs/enforcement.md) lists every failure mode, header, event and metric.
- **LiteLLM owns the hard limit:** `fail_closed_budget_enforcement` decides whether a window may close one request over budget. The trade-off is set out under [the envelope boundary](docs/enforcement.md#the-envelope-boundary).
- **Known limitations:** fallback entitlement reads only the direct key and team model lists, and a request refused at the budget carries no platform header. The [design notes](docs/design.md#known-limitations) list the rest.

## License

MIT
