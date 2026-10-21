# Changelog

All notable changes to litellm-brownout are listed here. Versions follow semantic versioning.

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
