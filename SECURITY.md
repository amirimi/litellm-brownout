# Security

## Reporting a vulnerability

Report a vulnerability through GitHub's private vulnerability reporting, under this repository's
Security tab. The report stays private until a fix is released.

Only the latest release receives fixes.

## What brownout reads and writes

The plugin runs inside the LiteLLM proxy. On each chat request its code takes the caller's team id,
model lists, budget flags and spend from LiteLLM's auth object, and the requested model's prices and
data classes from LiteLLM's router. It never reads the key, the prompt or a provider key. It writes
a `brownout` field into the request's `spend_logs_metadata`, `x-brownout-*` response headers, one
log line per event and claim keys in LiteLLM's own Redis. Each carries only team ids, route names,
deployment ids, tiers, amounts and reason codes. The plugin opens no network connection of its own.
A test plants a marker in every secret the handler can see and fails if the marker reaches any of
these outputs.

`brownout init` runs on the operator's machine. It reads `/team/list`, `/key/list` and `/model/info`
with a key that only needs read access, writes two new files, and prints no value from the gateway
config. `brownout check` reads the config and the manifest and prints rule ids and route names.

## Checking a release

Every file on PyPI carries an attestation that ties it to the workflow run in this repository that
built it.

```bash
uvx pypi-attestations verify pypi --repository https://github.com/amk9978/litellm-brownout \
  pypi:litellm_brownout-0.1.0-py3-none-any.whl
```

The documented install line pins an exact version, and PyPI never lets a published file be replaced.
Publishing needs the owner's approval in GitHub, and no PyPI token exists to steal.
