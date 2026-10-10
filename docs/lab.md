# Lab walkthrough

The lab stack runs the stock LiteLLM image with the plugin mounted, so every step below exercises a
real gateway on mock routes. You start the stack and create a fresh $1.00 team, send one request to
prove the plugin is in the path, watch three views in other terminals, drain the envelope, then
change one thing at a time.

The plugin needs no install step. `docker-compose.yml` mounts `src/brownout` at `/app/plugins`,
which is on `PYTHONPATH`, and `config/config.yaml` names the handler in
`callbacks: ["brownout.gateway.handler"]`, which LiteLLM imports at startup. The routes in
`config/config.yaml` answer with `mock_response`. The `langfuse` and `datadog_metrics` callbacks it
names run against the placeholder keys in `.env.example` without failing a request.

## 1. Start the stack and create a fresh team

Copy `.env.example` to `.env` once, then run these commands. Its passwords and master key are
placeholders, and every port the stack publishes binds to `127.0.0.1`, so the lab is reachable only
from the machine it runs on. Change both before exposing it anywhere else.

```bash
docker compose up -d --wait
set -a; source .env; set +a
KEY=$(uv run python lab/setup_lab.py | tail -1)
curl -s -X DELETE localhost:8088/events
```

`lab/setup_lab.py` resets the lab team in about 11 seconds, and the `DELETE` empties the event
sink. Rerun this step before every experiment, because the lab team and its claims carry over.

## 2. Send one request

```bash
ask() { curl -s -D - -o /dev/null localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer ${2:-$KEY}" -H "Content-Type: application/json" \
  -d "{\"model\":\"budget-lab-primary\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":${1:-40}}" \
  | grep -i '^HTTP/\|^x-brownout'; }
ask
```

An `x-brownout-route-served` header proves the plugin is in the path. The image logs at a level that
hides the plugin's own load message, so the header is the load check.

## 3. Watch three views

```bash
watch -n1 'curl -s localhost:8088/events | jq -c ".events[] | {event, scope, route, served_route, tier, degrade_at, rejection}"'
watch -n1 'uv run python -c "from tests.support import lab_spend_counter; print(lab_spend_counter(\"budget-lab\"))"'
watch -n1 'docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning --scan --pattern "brownout:*"'
```

The first shows the plugin's events, the second the dollar counter LiteLLM keeps for the team, and
the third the plugin's once-per-window claims in Redis. The third needs `.env` loaded in its shell.

## 4. Drain the envelope

```bash
for i in $(seq 1 70); do ask; done
```

On the last measured run, request 37 was the first downgrade, request 51 the first to carry tier 90,
and request 57 took LiteLLM's native 429 with $0.966 settled against a $0.0408 estimate. Expect the
numbers to move slightly between runs.

```
HTTP/1.1 200 OK
x-brownout-route-served: budget-lab-degraded
x-brownout-budget-tier: 75
x-brownout-degraded-from: budget-lab-primary
x-brownout-degradation-reason: degrade_tier_reached
```

## 5. Change one thing at a time

| Experiment | How | What you see |
|---|---|---|
| A refused downgrade | Create a key limited to the primary with `NARROW=$(curl -s localhost:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' -d '{"team_id":"budget-lab","models":["budget-lab-primary"]}' \| jq -r .key)`, drain past 75% with `$KEY`, then run `ask 40 $NARROW` | The primary serves with `x-brownout-degradation-reason: fallback_rejected`, and a `brownout_fallback_rejected` event carries `outside_caller_allowlist` |
| A later degrade tier | Set `degrade_at: 90` for `budget-lab-primary` in `config/manifest.yaml` and run `docker compose restart litellm` | The first downgrade arrives about 14 requests later |
| LiteLLM's boundary ahead of the tier | Drain with `ask 200` | The 429 arrives near 80%, because the larger worst case no longer fits, and a degrade tier at 90 never fires |
| A broken manifest | Set `budget_duration: "2d"` and restart | The container restarts in a loop, and `docker compose logs litellm` shows `ManifestError` naming `duration-tumbles` |
| A route tier | Add `warn_tiers: [50]` under `budget-lab-strict`, restart, then send requests to `budget-lab-strict` past 50% | A `brownout_tier_crossed` event with `scope: route` and an `x-brownout-route-tier: 50` header |
| A newer LiteLLM | Run `LITELLM_TAG=v1.104.2 docker compose up -d --wait` and repeat the drain | The same sequence on another release line |
| The file rules | Run `uv run brownout check --gateway-config config/config.yaml --manifest config/manifest.yaml`, then add `alerting: ["slack"]` under `general_settings` and run it again | The second run exits non-zero naming `no-native-alerting` |
