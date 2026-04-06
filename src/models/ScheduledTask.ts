import mongoose, { Schema, Document, Model } from 'mongoose';

export interface IScheduledTask extends Document {
  scheduled_task_id: string;
  user_id: string;
  instance_id: string;
  prompt: string;
  persona_id?: string;
  required_api_servers?: string[];
  schedule: string;
  timezone: string;
  enabled: boolean;
  label?: string;
  description?: string;
  max_turns?: number;
  next_run_at?: Date;
  last_executed_at?: Date;
  run_count: number;
  last_run?: {
    status: 'done' | 'failed';
    output?: string;
    error?: string;
    task_id?: string;
    started_at: Date;
    completed_at: Date;
    duration_ms?: number;
  };
  api_server_set_status?: 'clean' | 'dirty';
  pending_api_server_recommendations?: Array<{
    api_server: string;
    reason: string;
    recommended_at: Date;
    source_run_id?: string;
  }>;
  blocked_reason?: 'pending_api_approval' | null;
  consecutive_stable_count: number;
  graduation_eligible: boolean;
  graduation_threshold: number;
  graduated_at?: Date;
  graduation_action?: string;
  graduation_snapshot_task_id?: string;
  last_trajectory?: 'improving' | 'regressing' | 'stable';
  deleted_at?: Date;
  created_at: Date;
  updated_at: Date;
}

const ScheduledTaskSchema = new Schema<IScheduledTask>(
  {
    scheduled_task_id: { type: String, required: true, unique: true, index: true },
    user_id:           { type: String, required: true, index: true },
    instance_id:       { type: String, required: true, index: true },
    prompt:            { type: String, required: true },
    persona_id:        { type: String },
    required_api_servers: [{ type: String }],
    schedule:          { type: String, required: true },
    timezone:          { type: String, default: 'UTC' },
    enabled:           { type: Boolean, default: true, index: true },
    label:             { type: String },
    description:       { type: String },
    max_turns:         { type: Number, default: 30 },
    next_run_at:       { type: Date, index: true },
    last_executed_at:  { type: Date },
    run_count:         { type: Number, default: 0 },
    last_run: {
      status:       { type: String, enum: ['done', 'failed'] },
      output:       { type: String },
      error:        { type: String },
      task_id:      { type: String },
      started_at:   { type: Date },
      completed_at: { type: Date },
      duration_ms:  { type: Number },
    },
    api_server_set_status: { type: String, enum: ['clean', 'dirty'] },
    pending_api_server_recommendations: [{
      api_server:     { type: String, required: true },
      reason:         { type: String, required: true },
      recommended_at: { type: Date, required: true },
      source_run_id:  { type: String },
    }],
    blocked_reason: { type: String, enum: ['pending_api_approval'], default: null },
    consecutive_stable_count:    { type: Number, default: 0 },
    graduation_eligible:         { type: Boolean, default: false },
    graduation_threshold:        { type: Number, default: 5 },
    graduated_at:                { type: Date },
    graduation_action:           { type: String },
    graduation_snapshot_task_id: { type: String },
    last_trajectory:             { type: String, enum: ['improving', 'regressing', 'stable'] },
    deleted_at: { type: Date },
  },
  { timestamps: { createdAt: 'created_at', updatedAt: 'updated_at' } }
);

ScheduledTaskSchema.index({ user_id: 1, enabled: 1, next_run_at: 1 });
ScheduledTaskSchema.index({ instance_id: 1, enabled: 1, next_run_at: 1 });
ScheduledTaskSchema.index({ user_id: 1, deleted_at: 1, created_at: -1 });

export const ScheduledTask: Model<IScheduledTask> =
  mongoose.models.ScheduledTask || mongoose.model<IScheduledTask>('ScheduledTask', ScheduledTaskSchema, 'scheduled_tasks');
