<div align="center">

# LiteLLM Brownout

**Switch selected routes to cheaper models before the team budget runs out**

A team has $500 for the week, and at $450 its routine Q&A moves to a smaller model while code review keeps its primary.

</div>

## Install

The plugin is a Python package, `brownout`, that you mount into the stock LiteLLM image. It is
tested on LiteLLM 1.101 to 1.104. Clone the repository to get the package, the example config and a
lab stack. The lab needs Docker Compose, Python 3.12+, [uv](https://docs.astral.sh/uv/) and `curl`.

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
  | grep -i '^HTTP/\|^x-brownout'
done
```

`lab/setup_lab.py` creates a fresh lab team and prints its key, which takes about 11 seconds.
Early requests are served by `budget-lab-primary`. Around request 37 the team passes 75% and
responses switch to the fallback model.

```
HTTP/1.1 200 OK
x-brownout-route-served: budget-lab-degraded
x-brownout-budget-tier: 75
x-brownout-degraded-from: budget-lab-primary
x-brownout-degradation-reason: degrade_tier_reached
```

Around request 57 LiteLLM answers with its own 429, because the next request no longer fits the
budget. Run `curl -s localhost:8088/events | jq` to see the events the plugin raised on the way.

To repeat the run, rerun `lab/setup_lab.py` and the loop. The script deletes and recreates the
`budget-lab` team and clears its counters and event claims, so point it only at the lab gateway.
`docker compose down` stops the stack. The credentials in `.env.example` are for local development
only. The [lab walkthrough](docs/lab.md) covers more experiments, such as a refused downgrade and a
broken manifest.

## How it works

A team's spend passes the thresholds its manifest declares. Each warning tier raises one event. At
a route's `degrade_at` percentage, requests for that route are served by its fallback model, and
the response headers tell the caller. When the next request's worst-case cost no longer fits the
budget, LiteLLM refuses it with a 429. From LiteLLM 1.104 that refusal is a 422 by default, and
`budget_exceeded_status_code: 429` under `litellm_settings` restores the 429. The $500 team above
warns at 75% and 90%, and its Q&A route degrades at 90%.

| Team spend | Q&A route | Code review route | Signal |
|---|---|---|---|
| Below $375 | Primary | Primary | |
| $375 to $449.99 | Primary | Primary | 75% warning |
| $450 and above | Fallback, when the caller may use it | Primary | 90% warning, a route-degraded event and downgrade headers |
| Too little headroom for the requested route's worst case | Refused | Refused | LiteLLM's 429 |

Spend here is LiteLLM's team counter, minus the current request's own reservation. The counter
includes other requests' outstanding reservations, so it runs ahead of settled spend. The plugin
decides on every request and keeps no downgrade state between requests.

LiteLLM already reserves each request's worst-case cost, counts spend across replicas in Redis and
enforces the hard limit. The plugin adds four things its configuration cannot express.

- **Tier events at any threshold:** the team and each route declare their own warning tiers. Each raises one event per window, deduplicated across replicas. LiteLLM's built-in alerter is fixed at 85% and 95%.
- **Downgrade on the team total:** a route declares a fallback and the percentage at which to use it. LiteLLM's [budget fallbacks](https://docs.litellm.ai/docs/proxy/budget_fallbacks) switch only when a key exhausts a per-model budget.
- **Checked fallbacks:** a fallback must be served, priced and cleared for its route's data class, and the caller's key and team must be allowed to call it.
- **Caller disclosure:** `x-brownout-*` response headers name the route that served, the tiers reached and the reason for a downgrade.

`src/brownout/policy.py` makes every decision in two pure functions. `gateway.py` runs each request
through the checks and the decision, and `bridge.py` holds everything that touches LiteLLM.

## Assumptions

Brownout governs team budgets on one LiteLLM gateway. Each assumption below either has a rule that
enforces it, named by its id, or is documented and left unchecked. `brownout check` runs the file
rules in your CI, and the gateway runs the request rules on every governed request.

| Assumption | When it breaks | Enforced by |
|---|---|---|
| Budgets belong to teams, and every governed key belongs to a manifest team | 503 and a platform incident | `team-in-manifest` |
| Keys carry no budget of their own, whether `max_budget`, `model_max_budget` or `budget_limits` | 503 and a platform incident | `key-has-no-budget` |
| The team's LiteLLM `max_budget` equals `envelope_usd` | 503 and a platform incident | `ceiling-matches-envelope` |
| The team's LiteLLM `budget_duration` equals the manifest's | Tier windows drift from LiteLLM's resets | Documented, not checked |
| The window tumbles with LiteLLM's resets | The manifest refuses to load | `duration-tumbles` |
| No user, organisation, end-user or tag budget blocks first | LiteLLM refuses before any tier fires | Documented, not checked |
| No stacked team `budget_limits` | A daily limit inside a weekly one hits with no brownout first | Documented, not checked |
| The team's model list includes each fallback | The primary serves and a `brownout_fallback_rejected` event fires | The permission check |
| `fail_closed_budget_enforcement` is on, reservations are on, and no router fallbacks are set | 503 and a platform incident | `fail-closed-on`, `reservation-on`, `no-router-fallbacks` |
| LiteLLM's built-in alerting is off and Redis is configured | `brownout check` fails | `no-native-alerting`, `redis-configured` |
| Every manifest route and fallback is served and priced, and a fallback pairing's deployments declare one data class each | 503 and a platform incident, or `brownout check` fails | `route-served`, `route-priced`, `data-class-declared`, `fallback-data-class` |
| Only chat completions are governed | Embeddings, images and audio pass through untouched | Documented |
| The reset time occurs exactly once on every reset date | Daylight saving can move a window early or backward | Documented, not checked |
| `degrade_at` sits below 100% minus one worst-case request | The 429 arrives before the downgrade | Documented, not checked |
| LiteLLM is 1.101.0 or newer | The gateway refuses to start, and warns past the tested 1.104 | `supported-litellm` |

## Add it to your gateway

Four changes wire the plugin into your own gateway. Mount the package into the LiteLLM container,
point one environment variable at your manifest, name the handler in the gateway config, then
create each team in LiteLLM to match the manifest.

The callback governs every chat-completion call on the gateway, and a key whose team is missing
from the manifest gets a 503. On a shared gateway, add every team that sends chat traffic to the
manifest before you enable the callback.

The first two changes go in the LiteLLM service of your Compose file.

```yaml
services:
  litellm:
    image: docker.litellm.ai/berriai/litellm:v1.104.2
    environment:
      PYTHONPATH: /app/plugins
      BROWNOUT_MANIFEST: /app/config/manifest.yaml
    volumes:
      - ./src/brownout:/app/plugins/brownout:ro
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
  callbacks: ["brownout.gateway.handler"]
```

Every model group the manifest names needs input and output prices, either on its deployments or
from LiteLLM's cost map. A route with a fallback and the fallback itself also need a
`model_info.data_class` from the manifest's `data_class_order`. Leave `general_settings.alerting`,
`router_settings.fallbacks` and `disable_budget_reservation` out of the gateway config. Each
LiteLLM team needs the manifest's budget and duration and a model list that includes the fallback,
and its keys carry no budget of their own. The [configuration reference](docs/configuration.md)
explains every setting and gives the `curl` calls that create a team and key.

Run the file rules in CI before every deploy.

```bash
uv run brownout check --gateway-config config/config.yaml --manifest config/manifest.yaml
```

## Configuration

The manifest holds each team's policy, keyed by the LiteLLM `team_id`.

```yaml
version: "v1"

data_class_order:
  - internal
  - customer_pii

teams:
  team-a:
    policy_epoch: "team-a-v1"
    envelope_usd: 500.0
    budget_duration: "7d"
    warn_tiers: [75, 90]
    routes:
      chat-qa:
        fallback: chat-qa-degraded
        degrade_at: 90
        warn_tiers: [60, 80]
      code-review: {}
```

- **`envelope_usd` and `budget_duration`:** the budget the owner approved and its window. Both must equal the LiteLLM team's settings. `budget_duration` accepts only windows that tumble with LiteLLM's resets: `1h` to `24h` in divisors of 24, `1d`, `7d`, `1w`, `30d`, `1mo`, `hourly`, `daily`, `weekly` and `monthly`.
- **`warn_tiers`:** percentages of the team budget, optional at the team and at each route. A team tier raises an event for the budget owner, and a route tier raises one for the route's callers. Neither is derived from the other or from `degrade_at`.
- **`routes`:** each model group's fallback and the percentage at which to use it. A route with an empty policy or no entry keeps its primary model and still counts against the budget.
- **`data_class_order`:** data classes, least restricted first. Required when any route declares a fallback, which must be at least as restricted as its route.
- **`policy_epoch`:** a new value re-arms every tier inside the current window, for use after an approved policy change.

The handler reads the manifest once at startup, so a change needs a gateway restart. An unknown key
at any level stops the manifest from loading, so a typo such as `degrade_At` fails loudly.

## Notes

- **Supported LiteLLM:** 1.101.0 and newer, tested up to 1.104. The plugin imports three private LiteLLM helpers, so `src/brownout/bridge.py` refuses to start below the floor and warns past the tested ceiling. [CONTRIBUTING.md](CONTRIBUTING.md) explains how a new release joins the range.
- **Test the fallback first:** the plugin checks that a fallback is served, priced, cleared for the data class and callable by the key and team. Context size, tools, structured output and modalities stay unchecked, so try the fallback on your workload before real traffic. A dearer fallback is allowed, and it reaches LiteLLM's 429 sooner.
- **Failures refuse the request:** a broken assumption from the table above returns a 503 naming its rule id and raises a platform incident. The [enforcement reference](docs/enforcement.md) lists every failure mode, header, event and metric.
- **LiteLLM owns the hard limit:** `fail_closed_budget_enforcement` decides whether a window may close one request over budget. The trade-off is set out under [the envelope boundary](docs/enforcement.md#the-envelope-boundary).
- **Known limitations:** the [design notes](docs/design.md#known-limitations) list them.
- **Field reports:** trying it on a real workload? [Open an issue](https://github.com/amk9978/litellm-brownout/issues) with the model pair, budget window, threshold, LiteLLM version and whether the fallback stayed useful. Leave keys and private prompts out.

## License

[MIT](LICENSE)
