import mongoose, { Schema, Document } from 'mongoose';

export interface ISubagentTask extends Document {
  task_id: string;
  thread_id: string;
  user_id: string;
  label: string;
  status: 'running' | 'done' | 'failed' | 'cancelled';
  message: string;
  progress?: { step: number; note: string; updated_at: Date };
  result?: string;
  error?: string;
  persona_id?: string;
  max_turns: number;
  tokens?: { input: number; output: number; total: number };
  debug_log?: Array<{ ts: string; msg: string }>;
  started_at: Date;
  completed_at?: Date;
  response_consumed?: boolean;
  delivered_at?: Date;
  scheduled_task_id?: string;
  turns_used?: number;
  error_count?: number;
  api_servers_used?: string[];
  efficiency_status?: 'improvement_found' | 'no_further_tweaking_required';
  efficiency_next_run?: string;
  run_history_review_status?: 0 | 1;
  recommended_api_servers?: Array<{ api_server: string; reason: string }>;
  errors_encountered?: Array<{ api_server?: string; error_description: string; root_cause?: string; resolution: string }>;
  api_server_observations?: Array<{ api_server: string; observation: string }>;
  learnings_recorded_at?: Date;
  preload_task_pattern_id?: string;
  preload_efficiency_hint_id?: string;
  assessment?: {
    status: 'success' | 'partial' | 'failed';
    summary: string;
    outputs: Array<{ type: 'data' | 'action' | 'report' | 'notification'; description: string; detail?: string }>;
    errors: Array<{ description: string; severity: 'blocking' | 'non_blocking'; attempted_resolution?: string }>;
    follow_up_needed: boolean;
    follow_up_reason?: string;
    assumptions_made?: string[];
    submitted_at: Date;
  };
  self_improvement_assessment?: {
    performance_delta: 'better' | 'worse' | 'comparable' | 'first_run';
    evidence: string;
    lessons_confirmed: string[];
    lessons_invalidated: string[];
    new_lessons: string[];
  };
  performance_delta?: 'better' | 'worse' | 'comparable' | 'first_run';
  graphiti_ingested?: boolean;
}

const SubagentTaskSchema = new Schema<ISubagentTask>({
  task_id:    { type: String, required: true, unique: true, index: true },
  thread_id:  { type: String, required: true, index: true },
  user_id:    { type: String, required: true, index: true },
  label:      { type: String, required: true },
  status:     { type: String, enum: ['running', 'done', 'failed', 'cancelled'], default: 'running' },
  message:    { type: String, default: '' },
  progress:   { step: { type: Number }, note: { type: String }, updated_at: { type: Date } },
  result:     { type: String },
  error:      { type: String },
  persona_id: { type: String },
  max_turns:  { type: Number, default: 30 },
  tokens:     { input: { type: Number }, output: { type: Number }, total: { type: Number } },
  debug_log:  [{ ts: String, msg: String }],
  started_at:        { type: Date, default: Date.now },
  completed_at:      { type: Date },
  response_consumed: { type: Boolean, default: false },
  delivered_at:      { type: Date },
  scheduled_task_id: { type: String, index: true },
  turns_used:  { type: Number },
  error_count: { type: Number },
  api_servers_used:          [{ type: String }],
  efficiency_status:         { type: String, enum: ['improvement_found', 'no_further_tweaking_required'] },
  efficiency_next_run:       { type: String },
  run_history_review_status: { type: Number, enum: [0, 1] },
  recommended_api_servers:   [{ api_server: String, reason: String }],
  errors_encountered:        [{ api_server: String, error_description: String, root_cause: String, resolution: String }],
  api_server_observations:   [{ api_server: String, observation: String }],
  learnings_recorded_at:     { type: Date },
  preload_task_pattern_id:   { type: String },
  preload_efficiency_hint_id: { type: String },
  assessment: {
    status:    { type: String, enum: ['success', 'partial', 'failed'] },
    summary:   { type: String },
    outputs:   [{ type: { type: String, enum: ['data', 'action', 'report', 'notification'] }, description: String, detail: String }],
    errors:    [{ description: String, severity: { type: String, enum: ['blocking', 'non_blocking'] }, attempted_resolution: String }],
    follow_up_needed:  { type: Boolean },
    follow_up_reason:  { type: String },
    assumptions_made:  [{ type: String }],
    submitted_at:      { type: Date },
  },
  self_improvement_assessment: {
    performance_delta:   { type: String, enum: ['better', 'worse', 'comparable', 'first_run'] },
    evidence:            { type: String },
    lessons_confirmed:   [{ type: String }],
    lessons_invalidated: [{ type: String }],
    new_lessons:         [{ type: String }],
  },
  performance_delta: { type: String, enum: ['better', 'worse', 'comparable', 'first_run'] },
  graphiti_ingested: { type: Boolean, default: true },
});

SubagentTaskSchema.index({ user_id: 1, status: 1, started_at: -1 });

export const SubagentTask = mongoose.models.SubagentTask
  || mongoose.model<ISubagentTask>('SubagentTask', SubagentTaskSchema);
