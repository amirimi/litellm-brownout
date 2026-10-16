# Claude Code and Codex

Both tools reach the gateway with a team key and name model groups in place of their vendors' model
IDs. Point the tool at the gateway, name a route for every model it sends, and give it the context
window of the smaller model in that route's pairing.

## Claude Code

Claude Code sends `/v1/messages`.

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_AUTH_TOKEN=<A TEAM KEY>
export ANTHROPIC_MODEL=<A ROUTE>
export ANTHROPIC_DEFAULT_HAIKU_MODEL=<A ROUTE>
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=<THE SMALLER CONTEXT WINDOW>
claude
```

`ANTHROPIC_DEFAULT_HAIKU_MODEL` covers the background requests Claude Code makes, such as session
titles. Without it those requests name an Anthropic model ID, which the gateway refuses unless it
has a model group of that name. Set `ANTHROPIC_DEFAULT_SONNET_MODEL` and
`ANTHROPIC_DEFAULT_OPUS_MODEL` too if users switch models with `/model`. Claude Code cannot tell a
gateway model's context window and assumes 200,000 tokens, so `CLAUDE_CODE_MAX_CONTEXT_TOKENS` sets
the real one. Use the smaller window of the route and its fallback, because a downgraded session
keeps its history.

## Codex

Codex sends `/v1/responses`. Put this in `~/.codex/config.toml` and set `LITELLM_TEAM_KEY` to a team
key.

```toml
model = "<A ROUTE>"
model_provider = "litellm"
model_context_window = <THE SMALLER CONTEXT WINDOW>

[model_providers.litellm]
name = "LiteLLM gateway"
base_url = "http://localhost:4000/v1"
env_key = "LITELLM_TEAM_KEY"
wire_api = "responses"
```

Codex has no metadata for a gateway model name, warns about it and uses defaults.
`model_context_window` gives it the window to compact against.

## Tested

Both tools ran through a governed gateway on OpenRouter models, below the downgrade tier and past
it, and finished a tool-using task on the primary and on the fallback.
