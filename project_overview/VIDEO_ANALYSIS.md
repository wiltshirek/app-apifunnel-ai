# Video Analysis

The subagents service supports multimodal video analysis through two endpoints: a **transcript extraction** endpoint (pure infrastructure, no LLM) and the **one-shot agent node** (LLM-powered analysis of video content).

## Endpoints

| Endpoint | Purpose | LLM required? |
|---|---|---|
| `GET /v1/video/transcript?url=<url>` | Extract timestamped transcript from a video | No |
| `POST /v1/subagents/one-shot` | Analyze video with an LLM (frames + transcript + prompt) | Yes |

### GET /v1/video/transcript

Pure infrastructure. Extracts a timestamped transcript and returns it as text. No LLM, no reasoning, no inference.

1. Probes video metadata (duration, availability)
2. Attempts embedded/auto-generated captions via `yt-dlp`
3. If no captions: downloads audio, transcribes via Whisper (Groq first, OpenAI fallback)
4. Returns timestamped transcript with source metadata

**Cost**: Captions path is near-zero (metadata fetch only). Whisper fallback requires video download + API call — server-supplied keys, no cost to the caller.

```bash
curl -s "https://api.apifunnel.ai/v1/video/transcript?url=https://www.youtube.com/watch?v=VIDEO_ID" \
  -H "X-Admin-Key: $MCP_ADMIN_KEY" | jq .
```

Response shape:
```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "source": "captions",
  "duration_seconds": 516,
  "transcript": "00:00:00 Hey everyone...\n00:00:04 Today we're going to..."
}
```

### POST /v1/subagents/one-shot (video mode)

When the prompt contains a video URL (YouTube, Vimeo), the agent node automatically:

1. Runs the full preprocessing pipeline (download → frames → transcript)
2. Builds a multimodal content array (interleaved frames, transcript, user prompt)
3. Sends to the LLM (OpenAI, Anthropic, or Google — specified by caller's JWT)
4. Returns the LLM's analysis

The caller doesn't need to do anything special — just include a YouTube URL in the prompt. The pipeline detects it automatically.

```bash
curl -s https://api.apifunnel.ai/v1/subagents/one-shot \
  -H "Authorization: Bearer <user_jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Watch https://www.youtube.com/watch?v=VIDEO_ID and summarize the key points. Note any visual demonstrations and when they occur.",
    "model": "gpt-4o",
    "provider": "openai"
  }' | jq .
```

## Pipeline Architecture

```
Video URL
  │
  ├─ yt-dlp --dump-json ──────► Duration check (max 4 hours)
  │
  ├─ yt-dlp --write-auto-sub ─► Captions (VTT → timestamped text)
  │
  ├─ yt-dlp -f best[720p] ────► Video download (mp4, 720p max)
  │     │
  │     ├─ ffmpeg -vf fps ─────► Frame extraction (10–100 frames, JPEG, 720p)
  │     │
  │     └─ ffmpeg -vn ─────────► Audio extraction (mp3, for Whisper fallback)
  │           │
  │           └─ Whisper API ──► Transcription (Groq → OpenAI fallback)
  │
  └─ Multimodal content builder ► Provider-specific format (OpenAI/Anthropic/Google)
        │
        └─ LLM call ──────────► Analysis response
```

**Transcript-only endpoint** skips the download/frames/LLM path — goes straight from captions (or Whisper fallback) to response.

## System Dependencies

The video pipeline requires these on the host (installed in Dockerfile and on the Hetzner server):

| Tool | Purpose |
|---|---|
| `yt-dlp` | Video metadata, captions, download |
| `ffmpeg` | Frame extraction, audio extraction |
| `python3` | Required by `yt-dlp` internally |

Local dev: `brew install yt-dlp ffmpeg`

## Residential Proxy (Bright Data)

YouTube blocks datacenter IPs with bot detection. All `yt-dlp` calls route through a Bright Data residential proxy.

### How it works

The `PROXY_URL` env var is read by a `proxyArgs()` helper in `services/subagents/src/lib/video.ts`. When set, it adds `--proxy <url>` to every `yt-dlp` invocation. When unset, `yt-dlp` connects directly (works on residential IPs like local dev, fails on datacenter IPs).

### Credential format

```
http://brd-customer-<CUSTOMER_ID>-zone-<ZONE_NAME>:<PASSWORD>@brd.superproxy.io:22225
```

- **Customer ID**: From Bright Data dashboard (starts with `hl_`)
- **Zone name**: Must be a **Residential** zone (not Web Unlocker — Web Unlocker doesn't support CONNECT tunneling that `yt-dlp` needs)
- **Password**: Zone-specific password from the dashboard
- **Port 22225**: Standard residential proxy port (33335 is Web Unlocker only)

### KYC requirement

YouTube is a restricted site on Bright Data's residential network. Access requires completing their KYC form at `brightdata.com/cp/kyc`. Without it, all YouTube requests return `403 Forbidden` with error `policy_20050`.

### Env vars

```
PROXY_URL=http://brd-customer-hl_XXXXX-zone-residential_proxy1:PASSWORD@brd.superproxy.io:22225
```

Set in `.env` for local dev, in GitHub Actions secrets for production.

## Server-Supplied Keys

These keys are provided by the platform, not the caller. They power the transcript extraction (both standalone and as part of the agent node pipeline).

| Env var | Purpose | Required? |
|---|---|---|
| `GROQ_API_KEY` | Whisper transcription via Groq (preferred — faster, free tier) | At least one |
| `WHISPER_OPENAI_API_KEY` | Whisper transcription via OpenAI (fallback) | At least one |
| `PROXY_URL` | Bright Data residential proxy for `yt-dlp` | Required on datacenter IPs |

The caller's LLM key (OpenAI, Anthropic, Google) travels in their JWT via `api_settings` — see `PROVIDER_API_KEYS.md`. That key is only used for the agent node's LLM call, not for transcript extraction.

## Error Codes

All errors use the standard `LlmError` shape: `{ error: "<code>", message: "<details>" }`.

| Code | HTTP | Cause |
|---|---|---|
| `invalid_video_url` | 400 | Malformed or non-HTTP URL |
| `video_too_long` | 400 | Duration exceeds 4-hour maximum |
| `video_unavailable` | 422 | Private, deleted, geo-restricted, or bot-blocked |
| `transcript_unavailable` | 422 | No captions and no Whisper key configured |
| `transcript_failed` | 502 | Whisper API returned an error or empty result |
| `video_processing_failed` | 502 | `yt-dlp` or `ffmpeg` failure |
| `video_timeout` | 504 | Download or extraction exceeded timeout |

## File Inventory

| File | Role |
|---|---|
| `services/subagents/src/lib/video.ts` | Core pipeline: download, captions, frames, Whisper, proxy |
| `services/subagents/src/lib/multimodal.ts` | Builds provider-specific multimodal content arrays |
| `services/subagents/src/lib/llm-call.ts` | One-shot LLM call (detects video URLs, triggers pipeline) |
| `services/subagents/src/routes/video.ts` | `GET /v1/video/transcript` route handler |
| `services/subagents/openapi/subagents.yaml` | OpenAPI spec for both endpoints |
| `services/subagents/Dockerfile` | Installs `ffmpeg`, `python3`, `yt-dlp` |

## Testing

### Transcript endpoint (local)

```bash
cd services/subagents && npm run dev

curl -s "http://localhost:3001/v1/video/transcript?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  -H "X-Admin-Key: $MCP_ADMIN_KEY" | jq .
```

### Agent node with video (local)

```bash
curl -s http://localhost:3001/v1/subagents/one-shot \
  -H "Authorization: Bearer <jwt_with_api_settings>" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Analyze this video: https://www.youtube.com/watch?v=dQw4w9WgXcQ — What is happening visually at each timestamp?",
    "model": "gpt-4o",
    "provider": "openai"
  }' | jq .
```

### Production

Same calls against `https://api.apifunnel.ai`. Requires `PROXY_URL` set on the server for YouTube to work from the datacenter IP.

### Isolated proxy test

A Docker-based test harness exists at `test-proxy/` for validating `yt-dlp` + proxy in isolation without going through the full service stack. See `test-proxy/test.sh`.
