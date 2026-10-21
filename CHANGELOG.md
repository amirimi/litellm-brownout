# Changelog

All notable changes to litellm-brownout are listed here. Versions follow semantic versioning.

## 0.1.1 - 2026-10-22

Brownout now installs into LiteLLM's own image without a rebuild, runs on one LiteLLM process
without Redis, and announces in the gateway's log format so any log backend can alert on it.

### Changed

- **Install by mount, not by rebuild.** `init --write` copies the plugin into `plugins/` and, on
  Compose, writes an override file that mounts it into the LiteLLM service with `PYTHONPATH`. The
  README's Dockerfile is gone, and LiteLLM's image stays unchanged.
- **`init --write` edits the gateway config in place.** It keeps comments and permissions, checks
  the files it wrote, and no longer writes `brownout.config.yaml`.
- **`init` reads with any key** in `BROWNOUT_INIT_KEY` and sends `GET` requests only. The README no
  longer mints and deletes a viewer key.
- **Announcements use LiteLLM's log format.** With `JSON_LOGS=true`, each is one JSON line with
  `component: "brownout"`, `level` and its fields at the top level. Team events go to stdout and
  incidents to stderr, and `LITELLM_LOG` no longer hides them.
- **The manifest's default path is `brownout.yaml`** in the gateway's working directory, which is
  `/app/brownout.yaml` in LiteLLM's image.
- **Redis is optional on one LiteLLM process.** `redis-configured` is now a notice, because LiteLLM
  also takes Redis from a response cache or `REDIS_*` variables, and without any keeps an in-memory
  counter that it checks against the database. `init` derives `coordination_redis` from
  `router_settings` as well as from the cache. The handler logs on its first governed request
  whether the counter is shared through Redis or per process.

### Added

- `brownout plugin --dir plugins` rewrites only the plugin folder, for upgrades. The startup line
  names the loaded version.
- `init --platform helm` prints `proxy_config` settings and `litellm-helm` values, and
  `--platform pip` serves a proxy installed with pip or uv.
- `init --service`, `--compose-file`, `--redis-host` and `--plugin-dir`.

### Upgrading from 0.1.0

- A gateway that relied on the old default manifest path, `config/manifest.yaml`, sets
  `BROWNOUT_MANIFEST` to keep it.
- An alert that matched the `brownout {json}` line matches `component` and `event` instead.

### Supported

- LiteLLM 1.101.6 to 1.106. Pin its image by digest as well as tag.
- One LiteLLM process without Redis, or any number of processes with a Redis that LiteLLM uses.
  Several processes without Redis are unsupported.
- Python 3.12 and newer.

## 0.1.0 - 2026-10-21

The first release. Brownout is a LiteLLM callback that moves chosen routes to cheaper models as a
team's budget fills, and tells each caller which model answered.

### Added

- **Tier warnings at any percentage.** A team and each of its routes declare their own tiers. Each
  tier is announced once per budget window across every replica, as one JSON line on the
  `brownout` logger.
- **A downgrade on the team's total spend.** A route names a fallback and the `degrade_at`
  percentage at which to use it. Chat, text completion, `/v1/messages`, `/v1/responses` and Google's
  `generateContent` are downgraded, so Claude Code and Codex are covered. Embeddings, Codex's
  `/v1/responses/compact` and the other model calls keep the model they named and still raise the
  tier warnings.
- **Checked fallbacks.** A fallback must be served, priced and callable by the caller's key and
  team. A manifest that declares `data_class_order` also requires the fallback to be cleared for the
  route's data class. A fallback that fails a check leaves the primary serving and raises an event.
  A request from an agent key keeps its primary, because LiteLLM limits an agent's models in ways
  the key and team lists do not show.
- **Disclosure.** `x-brownout-*` response headers name the model group that served and the reason
  for a downgrade. The same facts reach LiteLLM's spend logs and every logging integration the
  gateway runs.
- **`brownout init`.** It reads a running gateway with a read-only key and lists every request
  brownout would refuse, each with the command or config lines that fix it. It then writes a
  manifest and a gateway config.
- **`brownout check`.** It runs the file rules in CI and prices each model the way the gateway bills
  it, through LiteLLM's router.
- **Fail-closed request rules.** A request that breaks a requirement gets a 503 naming its rule.
  This covers a key outside a manifest team, a key with a budget of its own, a model name that is
  not a configured group, an unpriced text or embedding model, a LiteLLM call type brownout has not
  reviewed, and an unreadable spend counter.

### Supported

- LiteLLM 1.101.6 to 1.106, pinned by image tag. The plugin refuses to start below 1.101.6 and
  warns above the tested 1.106.
- Python 3.12 and newer.
