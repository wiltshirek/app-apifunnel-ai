# Provider API Keys

How services on this platform access third-party API keys (OpenAI, Anthropic, Google, etc.) without inventing per-service auth infra.

## The Pattern

There is exactly one way a service gets a provider API key at runtime:

1. **JWT claims** — The platform (`one-mcp`) mints a user JWT with `api_settings` baked into the payload. The service decodes the JWT and reads the key from claims. This is the production path.

2. **Env var fallback** — For local development only. The service checks a well-known env var (e.g., `WHISPER_OPENAI_API_KEY`) when the JWT doesn't carry a key. This avoids requiring a full platform JWT mint for every `curl` test.

That's it. No body fields, no query params, no special headers, no per-endpoint key injection.

## How It Works

### Production (JWT path)

The platform mints a JWT containing:

```json
{
  "sub": "user_123",
  "api_settings": {
    "openai": { "api_key": "sk-..." },
    "anthropic": { "api_key": "sk-ant-..." },
    "google": { "api_key": "AI..." }
  }
}
```

The service reads it:

```python
# Python (lakehouse)
key = ident.api_settings.get("openai", {}).get("api_key")
```

```typescript
// TypeScript (subagents)
const key = getProviderKey(auth.api_settings, 'openai');
```

### Local Dev (env var fallback)

```python
api_key = _resolve_provider_key(api_settings, "openai") \
          or os.environ.get("WHISPER_OPENAI_API_KEY")
```

The env var is loaded from the repo root `.env` via `load_dotenv()`. If `load_dotenv` is called without an explicit path, it searches from CWD — which may not be the repo root if the service is started from its own directory. Always pass an explicit path:

```python
load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")
```

## What NOT to Do

- **Don't add provider keys to `_IDENTITY_FIELDS`** in the admin-key auth path. Admin-key callers pass identity (user_id, tenant_id, etc.), not secrets. Provider keys travel in JWTs only.

- **Don't accept API keys as request body fields.** This creates a new auth surface per endpoint. The key comes from the JWT or the environment — never from the caller's request body.

- **Don't add per-service env vars for the same key.** If two services need OpenAI, they both use `WHISPER_OPENAI_API_KEY` as the fallback. One key, one env var.

- **Don't invent new infra.** If you find yourself adding new fields to auth, new headers, or new key-passing mechanisms, stop. The pattern above already handles it. The JWT is the transport for user-scoped keys. The env var is the local dev escape hatch.

## Key Resolution Helper

Every service that needs provider keys should have a helper like this:

```python
def _resolve_provider_key(api_settings, provider):
    if not api_settings:
        return None
    bucket = api_settings.get(provider) or api_settings.get(provider.upper())
    if not bucket:
        return None
    if isinstance(bucket, str):
        return bucket
    return bucket.get("api_key") or bucket.get("apiKey") or bucket.get("key")
```

The subagents service has an equivalent `getProviderKey()` in TypeScript. Same logic, same field names.

## Testing with Admin Key

Admin-key callers (the platform) don't carry `api_settings` — that's by design. When testing an endpoint that needs a provider key via admin key auth, the service falls back to the env var. This means:

- `source .env` before starting the service, or fix the `load_dotenv` path
- The env var fallback is sufficient for local testing
- In production, the platform mints a JWT with `api_settings` and sends that

## Platform Infrastructure Keys

Some OpenAI usage is platform infrastructure, not user-scoped. These keys live in the environment and are never read from JWT `api_settings`:

| Env Var | Used By | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Lakehouse | Document & query embeddings for hybrid semantic search |

Do not confuse this with `WHISPER_OPENAI_API_KEY` (user-scoped fallback for provider calls). Embedding costs are absorbed by the platform.

## Current Services Using This Pattern

| Service | Provider Keys Used | Env Var Fallback |
|---|---|---|
| Subagents | OpenAI, Anthropic, Google (one-shot, video transcription) | `WHISPER_OPENAI_API_KEY` |
| Lakehouse | OpenAI (image generation) | `WHISPER_OPENAI_API_KEY` |
