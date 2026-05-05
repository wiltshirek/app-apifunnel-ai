/**
 * Video Editing API — shared type definitions.
 *
 * These mirror the data model from VIDEO_EDITING_API_PLAN.md and are used
 * across route handlers, workers, and Mongoose models.
 */

// ── Asset types ──────────────────────────────────────────────────────────────

export type AssetType =
  | 'video' | 'image' | 'audio' | 'pdf'
  | 'document' | 'spreadsheet' | 'subtitle' | 'unknown';

export type SourceKind = 'url' | 'lakehouse_asset' | 'upload' | 'generated' | 'data_url';

export interface AssetSource {
  kind: SourceKind;
  uri: string;
}

export interface PreparedAssetOutput {
  id: string;
  asset_id: string;
  type: 'image_sequence' | 'video_clip' | 'audio_clip' | 'subtitle_file';
  uri: string;
  width?: number;
  height?: number;
  duration_seconds?: number;
  pages?: number[];
}

export interface PrepareTarget {
  format?: 'png' | 'jpg' | 'webp';
  width?: number;
  height?: number;
  pages?: number[];
  fit?: 'contain' | 'cover' | 'fill';
  background?: string;
}

// ── Timeline types ───────────────────────────────────────────────────────────

export interface Transform {
  x?: number | string;
  y?: number | string;
  width?: number | string;
  height?: number | string;
  scale?: number;
  rotation?: number;
  crop?: {
    x: number | string;
    y: number | string;
    width: number | string;
    height: number | string;
  };
  anchor?: 'center' | 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right';
  fit?: 'contain' | 'cover' | 'fill';
}

export interface Keyframe {
  time: number;
  properties: {
    x?: number | string;
    y?: number | string;
    scale?: number;
    rotation?: number;
    opacity?: number;
    crop?: Transform['crop'];
    volume?: number;
  };
  easing?: 'linear' | 'ease_in' | 'ease_out' | 'ease_in_out';
}

export type TransitionType =
  | 'cut' | 'fade' | 'crossfade' | 'fade_to_black' | 'fade_from_black'
  | 'slide' | 'slide_in' | 'wipe' | 'dip_to_color' | 'push' | 'zoom';

export interface Transition {
  type: TransitionType;
  duration: number;
  color?: string;
  direction?: 'left' | 'right' | 'up' | 'down';
}

export interface Effect {
  type: string;
  params?: Record<string, unknown>;
}

export type ClipKind =
  | 'video' | 'image' | 'audio' | 'text'
  | 'subtitle' | 'generated' | 'blank' | 'effect';

export interface TimelineClip {
  id: string;
  asset_id?: string;
  kind: ClipKind;
  start: number;
  duration: number;
  source_start?: number;
  source_duration?: number;
  transform?: Transform;
  opacity?: number;
  volume?: number;
  transition_in?: Transition;
  transition_out?: Transition;
  effects?: Effect[];
  keyframes?: Keyframe[];
}

export type TrackType = 'video' | 'audio' | 'overlay' | 'subtitle' | 'effect';

export interface TimelineTrack {
  id: string;
  type: TrackType;
  clips: TimelineClip[];
}

export interface Timeline {
  tracks: TimelineTrack[];
}

// ── Compose types ────────────────────────────────────────────────────────────

export type InsertMode =
  | 'fullscreen' | 'overlay' | 'pip'
  | 'side_by_side' | 'background' | 'cutaway';

export interface KenBurnsAnimation {
  type: 'ken_burns';
  from: { scale: number; x: string; y: string };
  to: { scale: number; x: string; y: string };
}

export interface ComposeInsert {
  at: string | number;
  duration: number;
  asset: string;
  mode: InsertMode;
  position?: 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right' | 'center';
  size?: string;
  transition?: TransitionType;
  transition_duration?: number;
  animation?: KenBurnsAnimation;
  audio?: 'continue_source' | 'mute_source' | 'mix';
}

export interface ComposeRequest {
  source_video: string;
  inserts: ComposeInsert[];
  output: OutputSpec;
}

export interface OutputSpec {
  format?: 'mp4' | 'webm' | 'gif';
  width?: number;
  height?: number;
  fps?: number;
  quality?: 'low' | 'medium' | 'high';
}

// ── Job types ────────────────────────────────────────────────────────────────

export type JobType = 'prepare' | 'render' | 'compose' | 'generate';
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface JobResult {
  output_url?: string;
  outputs?: PreparedAssetOutput[];
  asset_id?: string;
  duration_seconds?: number;
  width?: number;
  height?: number;
  file_size_bytes?: number;
}
