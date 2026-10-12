<div align="center">

# LiteLLM Brownout

**Switch selected routes to cheaper models before the team budget runs out**

A team has $500 for the week, and at $450 its routine Q&A moves to a smaller model while code review keeps its primary.

It governs `/v1/chat/completions` and passes every other endpoint through untouched.

</div>

## Who this is for

Brownout fits a LiteLLM gateway that budgets teams and can meet these requirements across the whole
gateway, because the callback governs every chat completion it serves.

- Every key that sends chat traffic belongs to a team the manifest lists.
- No key carries a budget of its own.
- LiteLLM's built-in budget alerting is off.
- `fail_closed_budget_enforcement` is on and Redis is configured.
- LiteLLM runs a release from 1.95.1 to 1.104, pinned by image tag.

## Install

The plugin is a Python package, `brownout`, that you mount into the stock LiteLLM image. It is
tested on LiteLLM 1.95.1 to 1.104. Clone the repository to get the package, the example config and a
lab stack. The lab needs Docker Compose, Python 3.12+, [uv](https://docs.astral.sh/uv/), `curl` and
`jq`. The first `docker compose up` pulls the LiteLLM image, about 1.2 GB, and the first `uv run`
builds a local environment of about 680 MB and 117 packages, so the first start can take several
minutes.

```bash
git clone https://github.com/amk9978/litellm-brownout.git
cd litellm-brownout
cp .env.example .env
```

## Quickstart

The lab runs LiteLLM with the plugin mounted, alongside Postgres and Redis.
Those are real processes, and only the model responses and token prices are synthetic, so a run
spends no provider money. The lab team has a $1.00 budget, warning tiers at 75% and 90%, and a
primary route that downgrades at 75%.

```bash
docker compose up -d --wait
set -a; source .env; set +a
KEY=$(uv run python lab/setup_lab.py | tail -1)
[ -n "$KEY" ] || echo "lab/setup_lab.py printed no key. Fix the error above before the loop."
```

`lab/setup_lab.py` creates a fresh lab team and prints its key, which takes about 11 seconds. Once
it has printed a key, send the requests.

```bash
for i in $(seq 1 60); do
  curl -s -D - -o /dev/null localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"budget-lab-primary","messages":[{"role":"user","content":"hi"}],"max_tokens":40}' \
  | grep -i '^HTTP/\|^x-brownout'
done
```

Early requests are served by `budget-lab-primary`. Around request 38 the team passes 75% and
responses switch to the fallback model.

```
HTTP/1.1 200 OK
x-brownout-route-served: budget-lab-degraded
x-brownout-budget-tier: 75
x-brownout-degraded-from: budget-lab-primary
x-brownout-degradation-reason: degrade_tier_reached
```

Around request 57 LiteLLM answers with its own 429, because the next request no longer fits the
budget. Run `docker compose logs litellm | grep brownout_` to see the transitions the plugin announced
on the way.

To repeat the run, rerun `lab/setup_lab.py` and the loop. The script deletes and recreates the
`budget-lab` team and clears its counters and event claims, so point it only at the lab gateway.
`docker compose down` stops the stack. The credentials in `.env.example` are for local development
only. The [lab walkthrough](docs/lab.md) covers more experiments, such as a refused downgrade and a
broken manifest.

## How it works

A team's spend passes the thresholds its manifest declares. Each warning tier raises one event. At a
route's `degrade_at` percentage, requests for that route are served by its fallback model, and the
response headers tell the caller. When the next request's worst-case cost no longer fits the budget,
LiteLLM 1.101 and newer refuse it with a 429. LiteLLM 1.95.1 to 1.100 instead keep admitting
requests with their holds shrunk to the remaining headroom until none is left, so a window can close
above the budget. From LiteLLM 1.104 the refusal is a 422 by default, and the minimal gateway config
below sets `budget_exceeded_status_code: 429` under `litellm_settings` to keep it a 429. The $500 team above
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

- **Tier events at any threshold:** the team and each route declare their own warning tiers. Each is announced once per window on the gateway log, deduplicated across replicas. LiteLLM's built-in alerter is fixed at 85% and 95%.
- **Downgrade on the team total:** a route declares a fallback and the percentage at which to use it. LiteLLM's [budget fallbacks](https://docs.litellm.ai/docs/proxy/budget_fallbacks) switch only when a key exhausts a per-model budget.
- **Checked fallbacks:** a fallback must be served, priced and cleared for its route's data class, and the caller's key and team must be allowed to call it.
- **Caller disclosure:** `x-brownout-*` response headers name the route that served, the tiers reached and the reason for a downgrade. The same facts ride each request's LiteLLM log as `spend_logs_metadata.brownout`, so Langfuse, OpenTelemetry, Prometheus labels, the spend logs and any `CustomLogger` receive them with no plugin setting.

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
| `fail_closed_budget_enforcement` is on and reservations are on | 503 and a platform incident | `fail-closed-on`, `reservation-on` |
| A governed request is served only by the route the plugin chose | The plugin empties every router fallback list on each governed request, so gateway, key, team and caller fallbacks never move it. Failover between deployments of one route still works | Enforced on every request |
| LiteLLM's built-in alerting is off and Redis is configured | `brownout check` fails | `no-native-alerting`, `redis-configured` |
| Every manifest route and fallback is served and priced, and a fallback pairing's deployments declare one data class each | 503 and a platform incident, or `brownout check` fails | `route-served`, `route-priced`, `data-class-declared`, `fallback-data-class` |
| Only chat completions are governed | Embeddings, images, audio, `/v1/messages` and `/v1/responses` pass through untouched, at full price and without headers | Documented |
| The reset time occurs exactly once on every reset date | Daylight saving can move a window early or backward | Documented, not checked |
| `degrade_at` sits below 100% minus one worst-case request | The 429 arrives before the downgrade | Documented, not checked |
| LiteLLM is 1.95.1 or newer | The gateway refuses to start. It warns on 1.95.1 to 1.100, whose boundary can close a window above the budget, and past the tested 1.104 | `supported-litellm` |

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
  budget_exceeded_status_code: 429
```

`budget_exceeded_status_code` keeps LiteLLM's budget refusal a 429 from 1.104, whose default is a
422. Releases before 1.104 ignore it and refuse with a 429 anyway.

Every model group the manifest names needs input and output prices, either on its deployments or
from LiteLLM's cost map. A route with a fallback and the fallback itself also need a
`model_info.data_class` from the manifest's `data_class_order`. Leave `general_settings.alerting`
and `disable_budget_reservation` out of the gateway config. Router fallbacks may stay, and they
never apply to governed chat completions. Each
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

- **Supported LiteLLM:** 1.95.1 and newer, tested up to 1.104. The plugin imports three private LiteLLM helpers, so `src/brownout/bridge.py` refuses to start below the floor and warns past the tested ceiling. LiteLLM 1.95.1 to 1.100 shrink an over-budget reservation to the remaining headroom instead of refusing it. A window can then close above the envelope, by at most one request's cost under serial traffic, and the handler logs a warning at startup. Run 1.101 or newer for the hard boundary. [CONTRIBUTING.md](CONTRIBUTING.md) explains how a new release joins the range.
- **Pin the image tag:** a LiteLLM release that renames one of the three private helpers stops the gateway at startup. Pin the image tag and upgrade only to a release the weekly `litellm matrix` workflow has passed.
- **Image dependencies:** the LiteLLM images for 1.95.1 to 1.99.4 and for 1.101.0 ship PyJWT 2.13.0, urllib3 2.7.0, multidict 6.7.1 and oauthlib 3.3.1, which carry known advisories. The 1.100.5, 1.101.6, 1.102.4, 1.103.4 and 1.104.2 images ship fixed releases. On 1.95 to 1.99, upgrade those packages in your own image or move to a fixed patch.
- **Test the fallback first:** the plugin checks that a fallback is served, priced, cleared for the data class and callable by the key and team. Context size, tools, structured output and modalities stay unchecked, so try the fallback on your workload before real traffic. A dearer fallback is allowed, and it reaches LiteLLM's 429 sooner.
- **Redis is on the request path:** under `fail_closed_budget_enforcement`, LiteLLM refuses every budgeted request on the gateway while it cannot write a reservation to Redis, before the plugin runs. Losing the plugin's claim keys repeats every announcement in the current window. Run Redis with failover, persistence and the `noeviction` eviction policy.
- **Reporting trust:** the request facts are authoritative on every request the plugin handles. On a request LiteLLM refuses before the plugin runs, a caller can forge them, and LiteLLM attributes that request to the caller's own team. Build team dashboards on the facts and platform alerts on the `brownout` log line. The [enforcement reference](docs/enforcement.md#request-facts-and-announcements) covers both channels.
- **Failures refuse the request:** a broken assumption from the table above returns a 503 naming its rule id and raises a platform incident. The [enforcement reference](docs/enforcement.md) lists every failure mode, header and announcement.
- **LiteLLM owns the hard limit:** on LiteLLM 1.101 and newer, `fail_closed_budget_enforcement` decides whether a window may close above its budget. The trade-off is set out under [the envelope boundary](docs/enforcement.md#the-envelope-boundary).
- **Known limitations:** the [design notes](docs/design.md#known-limitations) list them.
- **Field reports:** trying it on a real workload? [Open an issue](https://github.com/amk9978/litellm-brownout/issues) with the model pair, budget window, threshold, LiteLLM version and whether the fallback stayed useful. Leave keys and private prompts out.

## License

[MIT](LICENSE)
