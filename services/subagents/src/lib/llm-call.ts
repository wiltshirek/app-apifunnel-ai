/**
 * One-shot LLM call — direct provider dispatch, no ReAct, no tools, no threads.
 *
 * The caller's JWT carries `api_settings` with provider keys. We pick the
 * provider from the model id (or an explicit override), call the provider's
 * HTTP API directly via fetch, and return structured JSON.
 *
 * Video media: when a video URL is detected in `media`, the video pipeline
 * (video.ts) extracts frames + transcript, then multimodal.ts builds the
 * provider-specific content array. Whisper keys are server-supplied env vars.
 *
 * Zero new npm deps — everything goes through fetch + child_process.
 */

import { isVideoUrl, preprocessVideo } from './video';
import { buildMultimodalContent } from './multimodal';

export type Provider = 'openai' | 'anthropic' | 'google';

export interface Media {
  url: string;
  type: string; // "image/png", "image/jpeg", "video/mp4", etc.
}

export interface OneShotOptions {
  prompt: string;
  model?: string;
  media?: Media[];
  temperature?: number;
  max_tokens?: number;
  api_settings: Record<string, any>;
}

export interface OneShotResult {
  text: string;
  tokens: { input: number; output: number; total: number };
  model: string;
  provider: Provider;
}

export class LlmError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function oneShotCompletion(opts: OneShotOptions): Promise<OneShotResult> {
  const model = opts.model || 'gpt-4o-mini';
  const provider = inferProvider(model);
  const apiKey = getProviderKey(opts.api_settings, provider);
  if (!apiKey) {
    throw new LlmError(400, 'missing_api_key', `No API key for provider "${provider}" in user api_settings.`);
  }

  const videoMedia = opts.media?.find(m => isVideoUrl(m.url, m.type));
  if (videoMedia) {
    const { frames, transcript } = await preprocessVideo(videoMedia.url);
    const multimodalContent = buildMultimodalContent(opts.prompt, frames, transcript, provider);
    const videoMaxTokens = opts.max_tokens ?? 4096;

    if (provider === 'openai') return callOpenAI(apiKey, model, opts, multimodalContent, videoMaxTokens);
    if (provider === 'anthropic') return callAnthropic(apiKey, model, opts, multimodalContent, videoMaxTokens);
    if (provider === 'google') return callGoogle(apiKey, model, opts, multimodalContent, videoMaxTokens);
    throw new LlmError(400, 'unknown_provider', `Cannot infer provider for model "${model}".`);
  }

  if (provider === 'openai') return callOpenAI(apiKey, model, opts);
  if (provider === 'anthropic') return callAnthropic(apiKey, model, opts);
  if (provider === 'google') return callGoogle(apiKey, model, opts);
  throw new LlmError(400, 'unknown_provider', `Cannot infer provider for model "${model}".`);
}

function inferProvider(model: string): Provider {
  const m = model.toLowerCase();
  if (m.startsWith('gpt-') || m.startsWith('o1') || m.startsWith('o3') || m.startsWith('o4')) return 'openai';
  if (m.startsWith('claude-')) return 'anthropic';
  if (m.startsWith('gemini-')) return 'google';
  return 'openai';
}

function getProviderKey(settings: Record<string, any>, provider: Provider): string | null {
  if (!settings) return null;
  const bucket = settings[provider] || settings[provider.toUpperCase()];
  if (!bucket) return null;
  if (typeof bucket === 'string') return bucket;
  return bucket.api_key || bucket.apiKey || bucket.key || null;
}

// ─── OpenAI ──────────────────────────────────────────────────────────────────

async function callOpenAI(
  apiKey: string, model: string, opts: OneShotOptions,
  videoContent?: any[], videoMaxTokens?: number,
): Promise<OneShotResult> {
  const messages: any[] = [];
  if (videoContent) {
    messages.push({ role: 'user', content: videoContent });
  } else if (opts.media && opts.media.length > 0) {
    const content: any[] = [{ type: 'text', text: opts.prompt }];
    for (const m of opts.media) {
      content.push({ type: 'image_url', image_url: { url: m.url } });
    }
    messages.push({ role: 'user', content });
  } else {
    messages.push({ role: 'user', content: opts.prompt });
  }

  const body: any = { model, messages };
  if (opts.temperature != null) body.temperature = opts.temperature;
  const effectiveMaxTokens = videoMaxTokens ?? opts.max_tokens;
  if (effectiveMaxTokens != null) body.max_tokens = effectiveMaxTokens;

  const timeout = videoContent ? 300_000 : 120_000;
  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${apiKey}`,
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeout),
  });

  if (!res.ok) {
    const err: any = await res.json().catch(() => ({}));
    throw new LlmError(res.status, 'openai_error', err?.error?.message || res.statusText);
  }

  const data: any = await res.json();
  const choice = data.choices?.[0]?.message;
  const text = typeof choice?.content === 'string'
    ? choice.content
    : Array.isArray(choice?.content)
      ? choice.content.filter((c: any) => c.type === 'text').map((c: any) => c.text).join('')
      : '';

  return {
    text,
    tokens: {
      input: data.usage?.prompt_tokens ?? 0,
      output: data.usage?.completion_tokens ?? 0,
      total: data.usage?.total_tokens ?? 0,
    },
    model: data.model || model,
    provider: 'openai',
  };
}

// ─── Anthropic ───────────────────────────────────────────────────────────────

async function callAnthropic(
  apiKey: string, model: string, opts: OneShotOptions,
  videoContent?: any[], videoMaxTokens?: number,
): Promise<OneShotResult> {
  let content: any[];
  if (videoContent) {
    content = videoContent;
  } else {
    content = [];
    if (opts.media && opts.media.length > 0) {
      for (const m of opts.media) {
        if (m.type.startsWith('image/')) {
          content.push({
            type: 'image',
            source: { type: 'url', url: m.url },
          });
        }
      }
    }
    content.push({ type: 'text', text: opts.prompt });
  }

  const body: any = {
    model,
    messages: [{ role: 'user', content }],
    max_tokens: videoMaxTokens ?? opts.max_tokens ?? 1024,
  };
  if (opts.temperature != null) body.temperature = opts.temperature;

  const timeout = videoContent ? 300_000 : 120_000;
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeout),
  });

  if (!res.ok) {
    const err: any = await res.json().catch(() => ({}));
    throw new LlmError(res.status, 'anthropic_error', err?.error?.message || res.statusText);
  }

  const data: any = await res.json();
  const text = Array.isArray(data.content)
    ? data.content.filter((c: any) => c.type === 'text').map((c: any) => c.text).join('')
    : '';

  return {
    text,
    tokens: {
      input: data.usage?.input_tokens ?? 0,
      output: data.usage?.output_tokens ?? 0,
      total: (data.usage?.input_tokens ?? 0) + (data.usage?.output_tokens ?? 0),
    },
    model: data.model || model,
    provider: 'anthropic',
  };
}

// ─── Google Gemini ───────────────────────────────────────────────────────────

async function callGoogle(
  apiKey: string, model: string, opts: OneShotOptions,
  videoContent?: any[], videoMaxTokens?: number,
): Promise<OneShotResult> {
  let parts: any[];
  if (videoContent) {
    parts = videoContent;
  } else {
    parts = [{ text: opts.prompt }];
    if (opts.media && opts.media.length > 0) {
      for (const m of opts.media) {
        if (m.type.startsWith('image/')) {
          parts.push({
            fileData: { mimeType: m.type, fileUri: m.url },
          });
        }
      }
    }
  }

  const effectiveMaxTokens = videoMaxTokens ?? opts.max_tokens;
  const body: any = {
    contents: [{ role: 'user', parts }],
    generationConfig: {
      ...(opts.temperature != null && { temperature: opts.temperature }),
      ...(effectiveMaxTokens != null && { maxOutputTokens: effectiveMaxTokens }),
    },
  };

  const timeout = videoContent ? 300_000 : 120_000;
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(apiKey)}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeout),
  });

  if (!res.ok) {
    const err: any = await res.json().catch(() => ({}));
    throw new LlmError(res.status, 'google_error', err?.error?.message || res.statusText);
  }

  const data: any = await res.json();
  const text = data.candidates?.[0]?.content?.parts?.map((p: any) => p.text || '').join('') ?? '';

  return {
    text,
    tokens: {
      input: data.usageMetadata?.promptTokenCount ?? 0,
      output: data.usageMetadata?.candidatesTokenCount ?? 0,
      total: data.usageMetadata?.totalTokenCount ?? 0,
    },
    model,
    provider: 'google',
  };
}
