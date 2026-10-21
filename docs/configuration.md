# Configuration

A governed gateway needs the plugin mounted into LiteLLM's container, the handler in its config, a
manifest entry for each team, and each team's LiteLLM `max_budget` equal to its manifest envelope.
`brownout init` sets all of this up. `brownout check` runs the file rules in CI, and the handler runs
the request rules on every governed request.

## Install

The plugin runs from a folder mounted into LiteLLM's own image, which stays unchanged. The gateway
needs three things.

| In the container | Value |
|---|---|
| The plugin folder | `plugins/`, mounted read-only at `/app/plugins` |
| `PYTHONPATH` | `/app/plugins` |
| The manifest | `brownout.yaml`, mounted read-only at `/app/brownout.yaml`, or wherever `BROWNOUT_MANIFEST` points |

`init --write` copies the plugin into `plugins/` and, on Compose, writes an override file that
supplies all three. `brownout plugin --dir plugins` rewrites only the folder, which is how an upgrade
works: run it from the new version, then restart the gateway. The startup line,
`brownout loaded: version=... teams=...`, names the version the gateway loaded.

Mount the folder anywhere except next to the gateway config. LiteLLM loads a callback it finds as
`brownout/gateway.py` beside `config.yaml` by file path, which breaks the plugin's imports.

| Variable | Default | Purpose |
|---|---|---|
| `BROWNOUT_MANIFEST` | `brownout.yaml` in the working directory, `/app/brownout.yaml` in LiteLLM's image | The manifest's path in the container |

The handler refuses to start below LiteLLM 1.101.6 and warns on a release newer than the tested
1.106. Request facts reach the logging integrations the gateway already runs, and announcements go
to the gateway log, as [the enforcement reference](enforcement.md#request-facts-and-announcements)
describes.

## Prepare with `brownout init`

`init` reads a running gateway, builds or reconciles the manifest, adds brownout's settings to the
gateway config, and lists what brownout would refuse. It issues only `GET` requests to
`/team/list`, `/key/list` and `/model/info`, and never writes to LiteLLM.

```bash
export BROWNOUT_INIT_KEY="{gateway key}"
brownout init --gateway http://localhost:4000 --gateway-config config.yaml --write
```

| Option | Default | Purpose |
|---|---|---|
| `--gateway` | required | The gateway's base URL |
| `--gateway-config` | required | The config the gateway runs today |
| `--manifest` | none | An existing manifest to reconcile. Its entries stay as written, and missing LiteLLM teams are appended |
| `--manifest-out` | `brownout.yaml` | Where the manifest is written |
| `--config-out` | the `--gateway-config` path | Where the prepared gateway config is written, comments and key order kept |
| `--plugin-dir` | `plugins` | Where the plugin folder is written |
| `--platform` | `compose` | `helm` writes only the manifest and prints the chart's settings and values. `pip` writes the config and manifest with nothing to mount |
| `--service` | the one service running LiteLLM's image | The Compose service the override extends |
| `--compose-file` | `compose.yaml` or `docker-compose.yml` in the current folder | The Compose file whose LiteLLM service the override extends |
| `--redis-host` | none | Writes `coordination_redis` for this host when the config names no Redis |
| `--write` | off | Write the files. Without it `init` prints the changes and the findings and writes nothing |

`init` reads its key only from `BROWNOUT_INIT_KEY`. Any key that can read teams, keys and models
works, the master key included, because `init` only reads. The commands it prints use
`$LITELLM_MASTER_KEY`. `init` never prints a value from the gateway config and never writes over an
existing manifest or override file.

Each finding prints as `rule-id<TAB>subject<TAB>detail`, followed by the command or YAML lines that
fix it. A value only you can choose, such as a team's budget, is a placeholder in angle brackets.

| Exit code | Meaning |
|---|---|
| `0` | Ready. The only finding is the notice that the admin keys cannot make model calls |
| `1` | Findings remain |
| `2` | The gateway or an input could not be read, an input is invalid, or an output cannot be written as asked |

### What `--write` creates

1. The gateway config gains the callback, `fail_closed_budget_enforcement`,
   `budget_exceeded_status_code: 429`, and a `coordination_redis` taken from a Redis cache or from
   `--redis-host`. Comments, key order and the file's permissions stay.
2. `brownout.yaml` is the manifest. It has one entry per LiteLLM team with a budget, copies that
   budget and window, warns at 75% and 90%, and declares no fallback yet.
3. `plugins/` holds the plugin and its version.
4. On Compose, `docker-compose.override.yml`, or `compose.override.yaml` next to a `compose.yaml`,
   extends the LiteLLM service with `PYTHONPATH` and the two mounts. Compose merges it on every
   `docker compose` command that runs without `-f`. A team that passes `-f` adds the override with a
   second `-f`, which `init` prints.
5. `init` runs `check` on the written files and prints the result.

The override keeps every other key of the service. A `PYTHONPATH` the service already sets, in
`environment` or `env_file`, keeps its entries with `/app/plugins` appended. `init` writes nothing
when it cannot tell which service runs LiteLLM, or when the service already mounts something at
`/app/plugins` or `/app/brownout.yaml`. When an override file already exists, `init` writes the rest
and prints the lines to merge into it. Without Docker on the machine, it prints the override instead
of writing it, and without a compose file it prints the `docker run` flags.

With `--platform helm`, `init` leaves the config alone, because LiteLLM's `litellm-helm` chart keeps
it under `proxy_config`. It writes the manifest and prints the settings to paste there, then chart
values for `extraInitContainers`, `volumes`, `volumeMounts` and `envVars`. The init container
installs the plugin from PyPI with `pip install --target`, so a cluster without PyPI access points
pip at its own mirror with `--index-url`. The last printed line creates the manifest's ConfigMap.

### Kubernetes with litellm-helm

1. Forward the gateway and save its config: `kubectl port-forward svc/litellm 4000:4000` and
   `helm get values litellm -o yaml | yq '.proxy_config' > config.yaml`.
2. Run `init` with `--platform helm --redis-host {redis service} --write`.
3. Create the ConfigMap with the command `init` prints.
4. Paste both printed blocks into your existing values file, then run `helm upgrade`. Add the
   printed `volumes` and `volumeMounts` to the lists you already have, because Helm replaces a list
   from a second values file.
5. Run `kubectl logs deploy/litellm -c litellm | grep "brownout loaded"`.

A manifest edit needs the ConfigMap updated, then `kubectl rollout restart deploy/litellm`. An
upgrade changes the version in the init container's command.

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
brownout check --gateway-config config.yaml --manifest brownout.yaml
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
