# Configuration

A governed gateway needs the package in its image, the handler in its config, a manifest entry for
each team, and each team's LiteLLM `max_budget` equal to its manifest envelope. `brownout check`
runs the file rules in CI, and the handler runs the request rules on every governed request.

## Install

```dockerfile
FROM docker.litellm.ai/berriai/litellm:v1.106.1
RUN python -m ensurepip --upgrade && python -m pip install --no-deps litellm-brownout==0.1.0
```

| Variable | Default | Purpose |
|---|---|---|
| `BROWNOUT_MANIFEST` | `config/manifest.yaml` | The manifest's path in the container |

The handler refuses to start below LiteLLM 1.101.6 and warns on a release newer than the tested
1.106. Request facts reach the logging integrations the gateway already runs, and announcements go
to the gateway log, as [the enforcement reference](enforcement.md#request-facts-and-announcements)
describes.

## Prepare with `brownout init`

`init` reads a running gateway with a read-only key, builds or reconciles the manifest, prepares the
gateway config, and lists what brownout would refuse. It issues only `GET` requests to
`/team/list`, `/key/list` and `/model/info`, and never writes to LiteLLM.

```bash
read -rs BROWNOUT_INIT_KEY && export BROWNOUT_INIT_KEY
brownout init --gateway http://localhost:4000 --gateway-config config.yaml --write
```

| Option | Default | Purpose |
|---|---|---|
| `--gateway` | required | The gateway's base URL |
| `--gateway-config` | required | The config the gateway runs today. `init` reads it and never edits it |
| `--manifest` | none | An existing manifest to reconcile. Its entries stay as written, and missing LiteLLM teams are appended |
| `--manifest-out` | `brownout.yaml` | Where the manifest is written |
| `--config-out` | `brownout.config.yaml` | Where the prepared gateway config is written, comments and key order kept |
| `--write` | off | Write both files. Without it `init` prints the changes and the findings and writes nothing |

A `proxy_admin_viewer` key is enough, and `init` reads it only from `BROWNOUT_INIT_KEY`. The
commands `init` prints use `$LITELLM_MASTER_KEY`, so the master key stays in your shell. `init`
never prints a value from the gateway config and never writes over an existing file.

Each finding prints as `rule-id<TAB>subject<TAB>detail`, followed by the command or YAML lines that
fix it. A value only you can choose, such as a team's budget, is a placeholder in angle brackets.

| Exit code | Meaning |
|---|---|
| `0` | Ready. The only finding is the notice that the admin keys cannot make model calls |
| `1` | Findings remain |
| `2` | The gateway or an input could not be read, an input is invalid, or an output path is taken |

An exit of `0` and a passing `brownout check` on the written files together mean that enabling the
callback refuses nothing `init` did not list.

### What `--write` creates

`--write` leaves the gateway's config as it was and creates two files.

- `brownout.yaml` is the manifest. It has one entry per LiteLLM team with a budget, copies that
  budget and window, warns at 75% and 90%, and declares no fallback yet. Pass `--manifest` to extend
  an existing one.
- `brownout.config.yaml` is the gateway config with comments kept. It adds the callback, turns on
  `fail_closed_budget_enforcement`, sets `budget_exceeded_status_code: 429` and derives
  `coordination_redis` from a Redis cache if there is one.

### OpenRouter prices

LiteLLM's cost map has no OpenRouter prices since 2026-10-09, so every OpenRouter model needs one in
the config. This command prints the two lines `init` asks for. Replace `anthropic/claude-haiku-4.5`
with the part of your model name after `openrouter/`.

```bash
{ curl -s https://openrouter.ai/api/v1/models; curl -s https://openrouter.ai/api/v1/embeddings/models; } \
  | jq -sr --arg m anthropic/claude-haiku-4.5 '[.[].data[] | select(.id == $m)][0]
      | "input_cost_per_token: \(.pricing.prompt)\noutput_cost_per_token: \(.pricing.completion)"'
```

## Gateway config

```yaml
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  fail_closed_budget_enforcement: true
  coordination_redis:
    host: os.environ/REDIS_HOST
    port: 6379
    password: os.environ/REDIS_PASSWORD

litellm_settings:
  callbacks: ["brownout.gateway.handler"]
  budget_exceeded_status_code: 429

model_list:
  - model_name: route-chat
    litellm_params:
      model: provider-x/large-model
      api_key: os.environ/PROVIDER_X_API_KEY
      input_cost_per_token: 0.000003
      output_cost_per_token: 0.000015
    model_info:
      data_class: customer_pii

  - model_name: route-chat-degraded
    litellm_params:
      model: provider-x/small-model
      api_key: os.environ/PROVIDER_X_API_KEY
      input_cost_per_token: 0.00000025
      output_cost_per_token: 0.00000125
    model_info:
      data_class: customer_pii
```

`provider-x` stands for a provider prefix LiteLLM recognises.

| Setting | What it does for brownout |
|---|---|
| `fail_closed_budget_enforcement: true` | LiteLLM refuses a request whose worst case does not fit the remaining budget, so spend stops at the envelope. Rule `fail-closed-on` |
| `coordination_redis` | Holds the team spend counter and brownout's announcement claims, so one crossing is one announcement across replicas. A Redis `cache_params` block alone fails rule `redis-configured`, and `init` writes `coordination_redis` from it |
| `callbacks` | Loads the handler |
| `budget_exceeded_status_code: 429` | Keeps LiteLLM's budget refusal a 429 instead of the 422 that 1.104 made the default |
| `input_cost_per_token`, `output_cost_per_token` | The prices LiteLLM reserves and bills against, in `litellm_params` or `model_info`. Rules `route-priced` and `model-priced` refuse a group with an unpriced deployment |
| `model_info.data_class` | Read only when the manifest declares `data_class_order`. Rules `data-class-declared` and `fallback-data-class` |

`check` prices each deployment the way the gateway bills it, so a `model_info.base_model` naming a
priced model prices a deployment with a custom name. `disable_budget_reservation` must stay off,
which rule `reservation-on` checks. Router fallbacks may stay in the config, and brownout disables
them on governed calls.

## Create the team

Every manifest team needs a LiteLLM team with the same `max_budget` and `budget_duration`, and a
model list that includes each fallback. Its keys carry no budget. `init` prints the request that
creates or corrects each team, and a new team and its key take these two.

```bash
curl -s localhost:4000/team/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "team-a", "team_alias": "team-a", "max_budget": 500.0, "budget_duration": "7d", "models": ["route-chat", "route-chat-degraded"]}'

curl -s localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "team-a", "key_alias": "team-a-app", "max_budget": null, "models": []}'
```

## Team manifest

```yaml
data_class_order:
  - internal
  - customer_pii

teams:
  team-a:
    envelope_usd: 500.0
    budget_duration: "7d"
    warn_tiers: [75, 90]
    routes:
      route-chat:
        fallback: route-chat-degraded
        degrade_at: 90
        warn_tiers: [60, 80]
      route-batch: {}
```

| Key | Type | Rule |
|---|---|---|
| `data_class_order` | list of distinct strings, least restricted first | Optional. Declaring it turns on the data-class rules for every fallback pairing |
| `teams.<team_id>` | mapping | The LiteLLM `team_id` |
| `envelope_usd` | positive number | Equals the LiteLLM team's `max_budget` |
| `budget_duration` | `1h`, `2h`, `3h`, `4h`, `6h`, `8h`, `12h`, `24h`, `1d`, `7d`, `1w`, `30d`, `1mo`, `hourly`, `daily`, `weekly` or `monthly` | Equals the LiteLLM team's. Other values fail rule `duration-tumbles` |
| `warn_tiers` | list of integers in 1..99, optional | Team tiers, each announced once per window |
| `routes.<model_group>` | mapping, may be empty | A LiteLLM model group. A model group without an entry still counts toward the team's tiers and never degrades. A wildcard group such as `openai/*` cannot be a route or a fallback |
| `routes.<model_group>.fallback` | model group name | The group that serves past `degrade_at`. Rules `fallback-distinct`, `route-served` and `route-priced` |
| `routes.<model_group>.degrade_at` | integer in 1..99 | Requires `fallback` |
| `routes.<model_group>.warn_tiers` | list of integers in 1..99, optional | Route tiers, each announced once per window on a request to the route |

Changing a team's envelope, window, tiers or routes re-arms its announcements for the current window
when the gateway restarts. An unknown key at any level fails rule `manifest-schema` and names the
key.

## The rules

`brownout check` runs the file rules, `init` adds the init rules against LiteLLM's live teams, and
the handler runs the manifest rules when it loads. A request rule refuses the request it fails. A
downgrade rule runs only when a request wants a downgrade, and a failure serves the primary.

| Id | Checks | Runs in |
|---|---|---|
| `manifest-schema` | Required keys, types, tier ranges and strict keys | Check, load |
| `duration-tumbles` | `budget_duration` is one of the accepted windows | Check, load |
| `fallback-distinct` | A fallback differs from its route | Check, load |
| `route-served` | Every manifest route and fallback is a gateway model group | Check, downgrade |
| `route-priced` | Every manifest route and fallback has input and output prices | Check, downgrade |
| `data-class-declared` | With `data_class_order` declared, a fallback pairing's deployments each declare one class from it | Check, downgrade |
| `fallback-data-class` | With `data_class_order` declared, a fallback is at least as restricted as its route | Check, downgrade |
| `fail-closed-on` | `fail_closed_budget_enforcement` is `true` | Check, request |
| `reservation-on` | `disable_budget_reservation` is absent or `false` | Check, request |
| `redis-configured` | `coordination_redis` is set | Check |
| `no-caller-routing` | The caller sent no fallback list | Request |
| `team-in-manifest` | The key's team is in the manifest | Request |
| `key-has-no-budget` | The key carries no `max_budget`, `model_max_budget` or `budget_limits` of its own | Request |
| `model-is-a-group` | The requested model is a model group the gateway configures | Request |
| `call-type-governed` | The call type is one brownout has reviewed | Request |
| `model-priced` | Every deployment of the requested model has a price LiteLLM bills | Request, routing |
| `ceiling-matches-envelope` | The team's `max_budget` equals `envelope_usd` | Request |
| `spend-readable` | The team's spend counter can be read | Request |
| `duration-matches` | A manifest team's `budget_duration` equals its LiteLLM team's | Init |
| `team-exists` | Every manifest team exists in LiteLLM | Init |
| `fallback-in-team-models` | Every fallback is in its team's LiteLLM model list, unless the list allows every model | Init |
| `member-has-no-budget` | No team member carries a budget inside the team, whether set per member or for every member | Init |
| `supported-litellm` | LiteLLM is 1.101.6 or newer | Load |

```bash
brownout check --gateway-config brownout.config.yaml --manifest brownout.yaml
```

`check` prints one line per violation and exits `1` when any rule fails, or `2` when an input cannot
be read. It follows the config's `include:` files the way LiteLLM does. With `--gateway URL`, it
also reads the models kept in LiteLLM's database through `/model/info`, with the key in
`BROWNOUT_INIT_KEY`.

## Fallback checks

`check` catches a bad fallback before deploy. The handler checks the fallback again whenever a
downgrade is wanted, because a model can be deleted, unpriced or reclassified after deploy, and it
asks LiteLLM whether the caller's key and team may call it. A fallback that fails either check
leaves the primary serving. The response discloses `fallback_rejected`, and
`brownout_fallback_rejected` names the rejection once per window. A permission check that cannot
run also raises an `entitlement_check_unavailable` incident.

## Data classes

Brownout can also refuse a fallback cleared for less sensitive data than its route, such as a public
provider standing in for a private deployment. The check runs once the manifest lists data classes,
least restricted first.

```yaml
data_class_order: [internal, customer_pii]
```

Every deployment of a route with a fallback, and of that fallback, then needs a
`model_info.data_class` from the list, and the fallback's class must be at least as restricted as
the route's. `check` names each deployment that lacks one. A model added in the admin UI takes its
class through LiteLLM's model update.

```bash
curl -sSf -X PATCH "http://localhost:4000/model/<MODEL ID>/update" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model_info": {"data_class": "internal"}}'
```
