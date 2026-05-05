import mongoose, { Schema, Document } from 'mongoose';
import type { AssetType, SourceKind, PreparedAssetOutput } from '../lib/video-edit/types';

export interface IVideoAsset extends Document {
  asset_id: string;
  user_id: string;
  type: AssetType;
  source_kind: SourceKind;
  source_uri: string;
  mime_type?: string;
  duration_seconds?: number;
  width?: number;
  height?: number;
  page_count?: number;
  prepared_outputs: PreparedAssetOutput[];
  metadata?: Record<string, unknown>;
  created_at: Date;
  ttl_expires_at?: Date;
}

const PreparedOutputSchema = new Schema({
  id:               { type: String, required: true },
  asset_id:         { type: String, required: true },
  type:             { type: String, enum: ['image_sequence', 'video_clip', 'audio_clip', 'subtitle_file'], required: true },
  uri:              { type: String, required: true },
  width:            { type: Number },
  height:           { type: Number },
  duration_seconds: { type: Number },
  pages:            [{ type: Number }],
}, { _id: false });

const VideoAssetSchema = new Schema<IVideoAsset>({
  asset_id:    { type: String, required: true, unique: true, index: true },
  user_id:     { type: String, required: true, index: true },
  type:        { type: String, enum: ['video', 'image', 'audio', 'pdf', 'document', 'spreadsheet', 'subtitle', 'unknown'], required: true },
  source_kind: { type: String, enum: ['url', 'lakehouse_asset', 'upload', 'generated', 'data_url'], required: true },
  source_uri:  { type: String, required: true },
  mime_type:   { type: String },
  duration_seconds: { type: Number },
  width:       { type: Number },
  height:      { type: Number },
  page_count:  { type: Number },
  prepared_outputs: { type: [PreparedOutputSchema], default: [] },
  metadata:    { type: Schema.Types.Mixed },
  created_at:      { type: Date, default: Date.now },
  ttl_expires_at:  { type: Date },
});

VideoAssetSchema.index({ ttl_expires_at: 1 }, { expireAfterSeconds: 0 });

export const VideoAsset = mongoose.models.VideoAsset
  || mongoose.model<IVideoAsset>('VideoAsset', VideoAssetSchema);
