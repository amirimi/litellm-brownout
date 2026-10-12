# Lab walkthrough

The lab stack runs the stock LiteLLM image with the plugin mounted, so every step below exercises a
real gateway on mock routes. You start the stack and create a fresh $1.00 team, send one request to
prove the plugin is in the path, watch three views in other terminals, drain the envelope, then
change one thing at a time.

The plugin needs no install step. `docker-compose.yml` mounts `src/brownout` at `/app/plugins`,
which is on `PYTHONPATH`, and `config/config.yaml` names the handler in
`callbacks: ["brownout.gateway.handler"]`, which LiteLLM imports at startup. The routes in
`config/config.yaml` answer with `mock_response`, and the config names no logging callback, so the
lab sends nothing off the machine.

## 1. Start the stack and create a fresh team

Copy `.env.example` to `.env` once, then run these commands. Its passwords and master key are
placeholders. The stack publishes the gateway on port 4000 and Redis on port 16379, both bound to
`127.0.0.1`, so the lab is reachable only from the machine it runs on and leaves the default Redis
port free. Change the credentials and the bindings before exposing it anywhere else.

```bash
docker compose up -d --wait
set -a; source .env; set +a
KEY=$(uv run python lab/setup_lab.py | tail -1)
```

`lab/setup_lab.py` resets the lab team and its claims in about 11 seconds. Rerun this step before
every experiment, because the lab team and its claims carry over.

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

The same facts reach every LiteLLM logging integration through `spend_logs_metadata`. LiteLLM's own
spend logs are one of them, keyed by the response's id and written in batches.

```bash
ID=$(curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"budget-lab-primary","messages":[{"role":"user","content":"hi"}],"max_tokens":40}' \
  | jq -r .id)
sleep 15
curl -s "localhost:4000/spend/logs?request_id=$ID" -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  | jq -c '.[0].metadata.spend_logs_metadata.brownout'
```

## 3. Watch three views

```bash
docker compose logs -f --no-log-prefix --since 1m litellm | sed -un 's/.*brownout \({.*}\)$/\1/p' | jq -c '{event, scope, route, served_route, tier, degrade_at, rejection}'
watch -n1 'docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning GET spend:team:budget-lab'
watch -n1 'docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning --scan --pattern "brownout:*"'
```

The first shows the transitions the plugin announces on the gateway log, the second the dollar
counter LiteLLM keeps for the team, and the third the plugin's once-per-window claims in Redis. The
second and third need `.env` loaded in their shells. The counter prints nothing once LiteLLM lets the
key expire.

## 4. Drain the envelope

```bash
for i in $(seq 1 70); do ask; done
```

On the last measured run, on LiteLLM 1.104.2, request 38 was the first downgrade, request 51 the
first to carry tier 90, and request 57 took LiteLLM's native 429 with $0.967 settled against a
$0.0408 estimate. Expect the
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
| A refused downgrade | Create a key limited to the primary with `NARROW=$(curl -s localhost:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' -d '{"team_id":"budget-lab","models":["budget-lab-primary"]}' \| jq -r .key)`, drain past 75% with `$KEY`, then run `ask 40 $NARROW` | The primary serves with `x-brownout-degradation-reason: fallback_rejected`, and the gateway log announces `brownout_fallback_rejected` with `outside_caller_allowlist` |
| A later degrade tier | Set `degrade_at: 90` for `budget-lab-primary` in `config/manifest.yaml` and run `docker compose restart litellm` | The first downgrade arrives about 14 requests later |
| LiteLLM's boundary ahead of the tier | Drain with `ask 200` | The 429 arrives near 80%, because the larger worst case no longer fits, and a degrade tier at 90 never fires |
| A broken manifest | Set `budget_duration: "2d"` and restart | The container restarts in a loop, and `docker compose logs litellm` shows `ManifestError` naming `duration-tumbles` |
| A route tier | Add `warn_tiers: [50]` under `budget-lab-strict`, restart, then send requests to `budget-lab-strict` past 50% | A `brownout_tier_crossed` announcement with `scope: route` and an `x-brownout-route-tier: 50` header |
| An older LiteLLM | Run `LITELLM_TAG=v1.95.1 docker compose up -d --wait` and repeat the drain | The gateway logs the `supported-litellm` boundary warning at startup. Requests that no longer fit are served with shrunk holds until no headroom is left. On the last measured run on 1.95.1 the 429 came at request 61 instead of 57, with $1.008 settled |
| The 422 default | Remove `budget_exceeded_status_code: 429` from `config/config.yaml` and run `docker compose restart litellm` | LiteLLM 1.104 refuses the exhausted budget with a 422 instead of a 429 |
| The file rules | Run `uv run brownout check --gateway-config config/config.yaml --manifest config/manifest.yaml`, then add `alerting: ["slack"]` under `general_settings` and run it again | The second run exits non-zero naming `no-native-alerting` |
