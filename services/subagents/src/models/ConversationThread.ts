import mongoose, { Schema, Document } from 'mongoose';

export interface IConversationThread extends Document {
  thread_id: string;
  user_id: string;
  agent_id: string;
  title?: string;
  messages: any[];
  reasoning_by_id?: Record<string, any>;
  cumulative_tokens?: { input: number; output: number; total: number };
  parent_thread_id?: string;
  is_subagent: boolean;
  status: 'active' | 'completed' | 'failed' | 'expired';
  created_at: Date;
  updated_at: Date;
  last_accessed_at: Date;
}

const ConversationThreadSchema = new Schema<IConversationThread>({
  thread_id:        { type: String, required: true, unique: true, index: true },
  user_id:          { type: String, required: true, index: true },
  agent_id:         { type: String, default: 'agent-v1' },
  title:            { type: String },
  messages:         { type: Schema.Types.Mixed, default: [] },
  reasoning_by_id:  { type: Schema.Types.Mixed },
  cumulative_tokens: {
    input:  { type: Number },
    output: { type: Number },
    total:  { type: Number },
  },
  parent_thread_id: { type: String },
  is_subagent:      { type: Boolean, default: false, index: true },
  status:           { type: String, enum: ['active', 'completed', 'failed', 'expired'], default: 'active' },
  created_at:       { type: Date, default: Date.now },
  updated_at:       { type: Date, default: Date.now },
  last_accessed_at: { type: Date, default: Date.now },
});

ConversationThreadSchema.index({ user_id: 1, is_subagent: 1, last_accessed_at: -1 });
ConversationThreadSchema.index({ parent_thread_id: 1 });

export const ConversationThread = mongoose.models.ConversationThread
  || mongoose.model<IConversationThread>('ConversationThread', ConversationThreadSchema);
