/**
 * Builds provider-specific multimodal content arrays from video frames + transcript.
 *
 * Layout: transcript (text) → frames interleaved with timestamp labels → user prompt.
 * This gives the model full transcript context before viewing frames, with
 * the user's question anchoring the response at the end.
 */

import type { Provider } from './llm-call';
import type { VideoFrame } from './video';

export function buildMultimodalContent(
  prompt: string,
  frames: VideoFrame[],
  transcript: string,
  provider: Provider,
): any[] {
  if (provider === 'anthropic') return buildAnthropic(prompt, frames, transcript);
  if (provider === 'google') return buildGoogle(prompt, frames, transcript);
  return buildOpenAI(prompt, frames, transcript);
}

function buildAnthropic(prompt: string, frames: VideoFrame[], transcript: string): any[] {
  const content: any[] = [];

  if (transcript) {
    content.push({ type: 'text', text: `Transcript:\n${transcript}` });
  }

  for (const frame of frames) {
    content.push({
      type: 'image',
      source: {
        type: 'base64',
        media_type: 'image/jpeg',
        data: frame.base64,
      },
    });
    content.push({ type: 'text', text: `[Frame at ${frame.timestamp}]` });
  }

  content.push({ type: 'text', text: prompt });
  return content;
}

function buildOpenAI(prompt: string, frames: VideoFrame[], transcript: string): any[] {
  const content: any[] = [];

  if (transcript) {
    content.push({ type: 'text', text: `Transcript:\n${transcript}` });
  }

  for (const frame of frames) {
    content.push({
      type: 'image_url',
      image_url: {
        url: `data:image/jpeg;base64,${frame.base64}`,
        detail: 'low',
      },
    });
    content.push({ type: 'text', text: `[Frame at ${frame.timestamp}]` });
  }

  content.push({ type: 'text', text: prompt });
  return content;
}

function buildGoogle(prompt: string, frames: VideoFrame[], transcript: string): any[] {
  const parts: any[] = [];

  if (transcript) {
    parts.push({ text: `Transcript:\n${transcript}` });
  }

  for (const frame of frames) {
    parts.push({
      inlineData: {
        mimeType: 'image/jpeg',
        data: frame.base64,
      },
    });
    parts.push({ text: `[Frame at ${frame.timestamp}]` });
  }

  parts.push({ text: prompt });
  return parts;
}
