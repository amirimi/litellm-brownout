# Configuration

A governed gateway needs four pieces in place before its first request. The `brownout` package is
mounted into the stock LiteLLM image, the gateway config names the handler, the manifest describes
each team, and each team exists in LiteLLM with a `max_budget` equal to its manifest envelope.
`brownout check` runs the file rules over the gateway config and the manifest in CI. The handler
validates the manifest when it loads and runs the request rules on every governed request.

## Mount the package

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

The plugin imports `pyyaml`, `fastapi` and `litellm`, all present in the image. Three of
its LiteLLM imports are private helpers (`_can_object_call_model`,
`_resolve_key_models_for_auth_check` and `_check_and_merge_model_level_guardrails`), and all of
them live in `src/brownout/bridge.py`. The handler refuses to start on LiteLLM below 1.95.1. It
logs one warning on 1.95.1 to 1.100, whose budget boundary is weaker, and one on a release newer
than the tested 1.104. CI runs both this mount and the README's wheel install.

The LiteLLM images for 1.95.1 to 1.99.4 and for 1.101.0 ship PyJWT 2.13.0, urllib3 2.7.0, multidict
6.7.1 and oauthlib 3.3.1, which carry known advisories. The 1.100.5, 1.101.6, 1.102.4, 1.103.4 and
1.104.2 images ship fixed releases. On 1.95 to 1.99, upgrade those packages in your own image or
move to a fixed patch.

| Variable | Default | Purpose |
|---|---|---|
| `BROWNOUT_MANIFEST` | `config/manifest.yaml` | Team budgets, tiers and route policies |

The plugin has no delivery settings. Request facts reach whatever logging integrations the gateway
already runs, and announcements go to the gateway log, as
[the enforcement reference](enforcement.md#request-facts-and-announcements) describes.

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

`read -rs` keeps the key off the screen and out of shell history, and it shows no prompt. `init`
reads its key only from `BROWNOUT_INIT_KEY`, which keeps the key off `init`'s command line and out
of the process list. A key for a `proxy_admin_viewer` user is enough. On 1.95.1 and 1.104.2 such a key reads all three
endpoints, sees provider keys in `/model/info` as null, and gets a 403 from any write. The commands
`init` prints use `$LITELLM_MASTER_KEY`, so the master key stays in the operator's shell and out of
`init`'s process. LiteLLM loads the nearest `.env` above its own install directory when `init`
imports it, so a `.env` there that holds the master key reaches `init`'s process too. `init` finds its
own key in `/key/list` and raises no team finding for it. A budget on that key is still a finding,
and the admin-keys notice names the key only when it has no team.

`init` refuses to write over an existing file or over one of its inputs. A config with a duplicate key stops
it, because LiteLLM's loader silently keeps the last value. Settings saved through LiteLLM's admin UI
live in its database, so the changes `init` lists cover only the file.

`init` never prints a value it read from the gateway config, because a config can hold secrets
inline. It lists each config change by its setting name, shows only the values it sets itself, and
reports an unreadable config by line and column. The prepared config gets the same file permissions
as the original. The manifest, which holds no secrets, prints as a diff.

Each finding prints as `rule-id<TAB>subject<TAB>detail`, followed by the command or the YAML lines
that resolve it. A value only the operator can choose, such as a team's budget, is left as a
placeholder in angle brackets, and LiteLLM refuses the command until it is filled in. A team id's
placeholder sits inside its JSON quotes, as in `"team_id": "<YOUR TEAM ID>"`, because LiteLLM
refuses a team that does not exist. A budget's placeholder stands bare, because a budget is a
number. A duration's placeholder also stands bare and asks for the quotes, because LiteLLM 1.95.1
stores a duration it cannot parse.

| Exit code | Meaning |
|---|---|
| `0` | Ready. The only finding is the notice that the admin keys cannot make chat calls |
| `1` | Findings remain |
| `2` | The gateway or an input could not be read, an input is invalid, or an output path is taken |

An exit of `0` and a passing `brownout check` on the written files together mean that enabling the
callback refuses nothing `init` did not list.

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

`provider-x` stands for a provider prefix LiteLLM recognises. Each key does one job.

| Setting | Why it is there |
|---|---|
| `fail_closed_budget_enforcement: true` | Decides the envelope boundary for the whole gateway, read from `general_settings` alone ([`proxy/proxy_server.py:2429`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/proxy_server.py#L2429)). On, a worst case that does not fit the remaining headroom is refused whole at authentication ([`proxy/spend_tracking/budget_reservation.py:141`](https://github.com/BerriAI/litellm/blob/v1.101.0/litellm/proxy/spend_tracking/budget_reservation.py#L141)), and so is a reservation that cannot be written or a spend figure that cannot be verified. LiteLLM 1.95.1 to 1.100 lack the first refusal and shrink the hold to the headroom instead. Rule `fail-closed-on` requires it |
| `coordination_redis` | The spend counter LiteLLM increments per team. The plugin's claims reuse the same Redis, so one crossing is one announcement across every replica. Rule `redis-configured` requires it in `brownout check`. A Redis `cache_params` block does not substitute for it, because on LiteLLM 1.95.1 it leaves each replica counting alone. A coordination Redis saved from the admin UI lives in the database, where the check cannot see it |
| `callbacks` | The dotted path to the handler `src/brownout/gateway.py` builds at import |
| `budget_exceeded_status_code: 429` | Keeps LiteLLM's budget refusal a 429 from 1.104, whose default is a 422 (`litellm/__init__.py:404` in 1.104.2). Releases before 1.104 ignore it and refuse with a 429 anyway. Run on the 1.104.2 lab image |
| `input_cost_per_token`, `output_cost_per_token` | What LiteLLM reserves against. A deployment without them is priced from LiteLLM's cost map. A model group counts as priced only when every deployment has both prices and one of them is above zero. Rules `route-priced` and `model-priced` refuse any other group |
| `model_info.data_class` | Read for a route that declares a fallback and for the fallback. Every deployment of the group declares the same class, from the manifest's `data_class_order`. Rules `data-class-declared` and `fallback-data-class` |

`general_settings.disable_budget_reservation` must not be `true`, because that would remove the hold
from every budget on the gateway, which rule `reservation-on` refuses. LiteLLM's own alerting may stay on
for outages, latency and deployments. Its budget alerts must stay off, because LiteLLM's hardcoded
85% and 95% budget alerter would become a second owner of tier announcements. Rule
`no-native-budget-alerts` fails when `general_settings.alerting` is set and `alert_types` is absent
or includes `budget_alerts`, so list the alert types the gateway wants without `budget_alerts`. An
`alert_types` list saved from the admin UI lives in the database, where the check cannot see it.

Router fallbacks may stay in the config, and they never apply to governed chat completions. On every
governed request the plugin sets `fallbacks`, `context_window_fallbacks` and
`content_policy_fallbacks` to empty lists. LiteLLM's router prefers a request's own list to its
configured one, and it merges key and team `router_settings` only for keys the request does not
already hold, so gateway, `default_fallbacks`, key, team and caller fallbacks all stay inert.
Failover between deployments of one model group, by `order` and cooldown, still works.

## Create the team

Every team in the manifest needs a LiteLLM team whose `max_budget` equals `envelope_usd` and whose
`budget_duration` equals the manifest's, with a model list that includes every declared fallback.
Keys carry no budget of their own, because a key budget would be a second ceiling the manifest
knows nothing about. `brownout init` prints the request that creates or corrects each team it finds
missing or drifted. A new team and its key take these two requests.

```bash
curl -s localhost:4000/team/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "team-a", "team_alias": "team-a", "max_budget": 500.0, "budget_duration": "7d", "models": ["route-chat", "route-chat-degraded"]}'

curl -s localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "team-a", "key_alias": "team-a-app", "max_budget": null, "models": []}'
```

The plugin checks the amount and the key budget on every request, and refuses with a 503 and a
platform incident when either breaks its assumption. The duration is invisible at request time, so
`brownout init` reports a drifted `budget_duration` as `duration-matches` before deploy.

## Team manifest

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
      route-chat:
        fallback: route-chat-degraded
        degrade_at: 90
        warn_tiers: [60, 80]
      route-batch: {}
```

| Key | Type | Rule |
|---|---|---|
| `version` | string | Required. Reported in the handler's load log |
| `data_class_order` | list of distinct strings, least restricted first | Required when any route declares a fallback |
| `teams.<team_id>` | mapping | Keyed by the LiteLLM `team_id`. A key whose team is absent here is refused with a 503 and a platform incident |
| `policy_epoch` | string | Required. Part of every claim key, so an approved policy change re-arms every transition inside the current window |
| `envelope_usd` | positive finite number | The owner-approved budget. Must equal the LiteLLM team's `max_budget` |
| `budget_duration` | `1h`, `2h`, `3h`, `4h`, `6h`, `8h`, `12h`, `24h`, `1d`, `7d`, `1w`, `30d`, `1mo`, `hourly`, `daily`, `weekly` or `monthly` | The windows that tumble with LiteLLM's resets. Any other value fails rule `duration-tumbles` |
| `warn_tiers` | list of integers in 1..99, optional | Team tiers. Each raises one event with scope `team` per window. 100 belongs to LiteLLM's ceiling |
| `routes.<model_group>` | mapping, may be empty | Keyed by LiteLLM model group name. A route absent from the manifest still spends against the envelope and still earns team tier events. It never degrades. A wildcard group such as `openai/*` cannot be a route or a fallback, because requests name concrete models and never match it |
| `routes.<model_group>.fallback` | model group name | The degradation target. It differs from its route (rule `fallback-distinct`), is served and priced, and is at least as restricted in data class. Its price relative to the route is the owner's call |
| `routes.<model_group>.degrade_at` | integer in 1..99 | Requires `fallback`. A degrade threshold only, and never a tier |
| `routes.<model_group>.warn_tiers` | list of integers in 1..99, optional | Route tiers. Each raises one event with scope `route` per window, on a request to that route |

An unknown key at any level fails rule `manifest-schema` and names the key, so a typo such as
`degrade_At` stops the manifest from loading. A team id or a route name must be a string. YAML reads
an unquoted number, or `yes`, `no`, `on` and `off`, as another type, so quote such a name, or rule
`manifest-schema` refuses it.

## The rules

Each rule has an id. `brownout check` runs the file rules, `brownout init` also runs the init rules
against LiteLLM's live teams, and the handler runs the manifest rules when it loads. The request rules run on every governed request against LiteLLM's live Router and
settings, and a violation refuses the request. The downgrade rules run against the live Router only
when a request wants a downgrade, and a violation withholds the downgrade and serves the primary.
Each rule reads only what its request touches, which is the route, its fallback and the gateway
settings.

| Id | Checks | Runs in |
|---|---|---|
| `manifest-schema` | Required keys, types, tier ranges and strict keys | Check, load |
| `duration-tumbles` | `budget_duration` is one of the accepted windows | Check, load |
| `fallback-distinct` | A fallback differs from its route | Check, load |
| `route-served` | Every manifest route and fallback is a gateway model group | Check, downgrade |
| `route-priced` | Every manifest route and fallback has input and output prices | Check, downgrade |
| `data-class-declared` | A fallback pairing's deployments each declare one class from `data_class_order` | Check, downgrade |
| `fallback-data-class` | A fallback is at least as restricted as its route | Check, downgrade |
| `fail-closed-on` | `fail_closed_budget_enforcement` is `true` | Check, request |
| `reservation-on` | `disable_budget_reservation` is absent or `false` | Check, request |
| `no-native-budget-alerts` | LiteLLM alerting is off, or `alert_types` is set and excludes `budget_alerts` | Check |
| `redis-configured` | `coordination_redis` is set | Check |
| `no-caller-routing` | The caller sent no fallback list | Request |
| `team-in-manifest` | The key's team is in the manifest | Request |
| `key-has-no-budget` | The key carries no `max_budget`, `model_max_budget` or `budget_limits` of its own | Request |
| `model-priced` | The requested model is priced | Request |
| `ceiling-matches-envelope` | The team's `max_budget` equals `envelope_usd` | Request |
| `spend-readable` | The team's spend counter can be read | Request |
| `duration-matches` | A manifest team's `budget_duration` equals its LiteLLM team's | Init |
| `team-exists` | Every manifest team exists in LiteLLM | Init |
| `fallback-in-team-models` | Every fallback is in its team's LiteLLM model list, unless the list allows every model | Init |
| `member-has-no-budget` | No team member carries a budget inside the team, whether set per member or for every member | Init |
| `supported-litellm` | LiteLLM is 1.95.1 or newer | Load |

```bash
uv run brownout check --gateway-config config/config.yaml --manifest config/manifest.yaml
```

The command prints one line per violation, as rule id, subject and message, and exits `1` when any
rule fails. An input that is missing, unreadable, not UTF-8, not valid YAML or not a YAML mapping
exits `2`. So does a config LiteLLM could not load as written, such as a `model_list` that is not a
list or a deployment without a `model_name`, and the message names the field. The message names the
file and gives at most a line and column or a byte offset, because the offending line can hold a
secret. It reads `config.yaml` as YAML, follows its `include:` files the way LiteLLM does, and fills
a deployment's missing prices from LiteLLM's cost map, so it runs without starting a gateway. An
included file that includes more files, or a file listed twice, exits `2`, because LiteLLM resolves
both differently across releases. A price written as a string, such as `1e-4`, counts when it reads
as a finite number, as it does for LiteLLM. Any other written price exits `2`, including an
`os.environ/` reference, because the check cannot know what it resolves to.

Models added in LiteLLM's admin UI live in its database, where a file cannot show them. With
`--gateway URL`, `check` also reads `/model/info` with the key in `BROWNOUT_INIT_KEY` and adds every
model group the files do not define. A group defined in a file keeps the file's prices and classes.
`init` always reads them this way.

## Fallback checks

A declared fallback is checked in two places, and the permission check runs last.

The rules cover what the gateway config and the manifest decide. The fallback is served, priced,
distinct from its route and at least as restricted in data class. `brownout check` catches a bad
pairing before deploy. On a running gateway, an admin can delete a model or change its data class
without any notice to the plugin, so the plugin checks the pairing again whenever a downgrade is
wanted. A broken pairing withholds the downgrade. The primary serves the request it was asked for,
the response discloses `fallback_rejected`, and a `gateway_rule_violated` incident names the rule
once per window. A request below `degrade_at` never touches the pairing, so a broken fallback costs
the team its downgrade and nothing else.

The permission check asks LiteLLM's own `_can_object_call_model` whether both the key and the team
may call the fallback. It runs only when the decision wants a downgrade, so a request below
`degrade_at` never touches it. A refusal serves the primary. The response header and the tier
event carry the outcome, and a `brownout_fallback_rejected` event names the route, the target and
the rejection on its own claim per route and rejection per window. A check that raises anything
other than LiteLLM's `ProxyException` is a platform fault, so the plugin serves the primary and
raises an `entitlement_check_unavailable` incident alongside the rejection event.
