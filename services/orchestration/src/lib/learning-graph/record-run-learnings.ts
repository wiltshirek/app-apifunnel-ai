import { connectDB } from '../db';
import { ScheduledTask } from '../../models/ScheduledTask';
import { SubagentTask } from '../../models/SubagentTask';
import { ingestLearning } from './client';

export interface LearningErrorEntry {
  api_server?: string;
  error_description: string;
  root_cause?: string;
  resolution: string;
}

export interface ApiServerObservation {
  api_server: string;
  observation: string;
}

export interface RecommendedApiServer {
  api_server: string;
  reason: string;
}

export interface RunLearningsPayload {
  api_servers_used: string[];
  errors_encountered: LearningErrorEntry[];
  api_server_observations: ApiServerObservation[];
  efficiency_status: 'improvement_found' | 'no_further_tweaking_required';
  efficiency_next_run: string;
  run_history_review_status: 0 | 1;
  recommended_api_servers: RecommendedApiServer[];
  prompt_text?: string;
  subagent_task_id?: string;
  scheduled_task_id?: string;
}

const REQUIRED_LEARNING_FIELDS: Array<keyof RunLearningsPayload> = [
  'api_servers_used',
  'errors_encountered',
  'api_server_observations',
  'efficiency_status',
  'efficiency_next_run',
  'run_history_review_status',
  'recommended_api_servers',
];

export function hasRunLearnings(payload: Partial<RunLearningsPayload>): boolean {
  return REQUIRED_LEARNING_FIELDS.some((field) => payload[field] !== undefined && payload[field] !== null);
}

export function validateRunLearnings(payload: Partial<RunLearningsPayload>): string | null {
  for (const field of REQUIRED_LEARNING_FIELDS) {
    if (payload[field] === undefined || payload[field] === null) {
      return `Field '${field}' is required when recording run learnings.`;
    }
  }

  if (payload.efficiency_status !== 'improvement_found' && payload.efficiency_status !== 'no_further_tweaking_required') {
    return "Field 'efficiency_status' must be 'improvement_found' or 'no_further_tweaking_required'.";
  }

  if (payload.run_history_review_status !== 0 && payload.run_history_review_status !== 1) {
    return "Field 'run_history_review_status' must be 0 (reviewed) or 1 (skipped).";
  }

  if (payload.efficiency_status === 'no_further_tweaking_required' && payload.efficiency_next_run !== 'No further tweaking required.') {
    return "When efficiency_status is 'no_further_tweaking_required', efficiency_next_run must be exactly: 'No further tweaking required.'";
  }

  return null;
}

export async function recordRunLearnings(params: {
  auth: { sub: string; scheduled_task_id?: string; subagent_task_id?: string };
  payload: RunLearningsPayload;
  scheduledTaskId?: string;
  subagentTaskId?: string;
  finalStatus?: 'done' | 'failed';
  skipGraphiti?: boolean;
}): Promise<{ dirtyTriggered: boolean }> {
  const { auth, payload, finalStatus, skipGraphiti } = params;
  const subagentTaskId = params.subagentTaskId ?? auth.subagent_task_id ?? payload.subagent_task_id;
  const scheduledTaskId = params.scheduledTaskId ?? auth.scheduled_task_id ?? payload.scheduled_task_id;

  await connectDB();

  let subagentTask: any = null;
  if (subagentTaskId) {
    subagentTask = await SubagentTask.findOne({ task_id: subagentTaskId, user_id: auth.sub });
  }

  let scheduledTask: any = null;
  if (scheduledTaskId) {
    scheduledTask = await ScheduledTask.findOne({
      scheduled_task_id: scheduledTaskId,
      user_id: auth.sub,
      deleted_at: { $exists: false },
    });
  }

  const recommendedServers = Array.isArray(payload.recommended_api_servers) ? payload.recommended_api_servers : [];
  let dirtyTriggered = false;

  if (scheduledTask && recommendedServers.length > 0) {
    const approvedSet = new Set<string>(scheduledTask.required_api_servers ?? []);
    const newServers = recommendedServers.filter((r: any) => !approvedSet.has(r.api_server));

    if (newServers.length > 0) {
      dirtyTriggered = true;
      const pendingRecs = newServers.map((r: any) => ({
        api_server: r.api_server,
        reason: r.reason,
        recommended_at: new Date(),
        source_run_id: subagentTaskId,
      }));

      await ScheduledTask.findOneAndUpdate(
        { scheduled_task_id: scheduledTaskId, user_id: auth.sub },
        {
          $set: { api_server_set_status: 'dirty', blocked_reason: 'pending_api_approval' },
          $push: { pending_api_server_recommendations: { $each: pendingRecs } },
        }
      );
    }
  }

  if (subagentTask) {
    await SubagentTask.findOneAndUpdate(
      { task_id: subagentTaskId, user_id: auth.sub },
      {
        $set: {
          api_servers_used: payload.api_servers_used,
          efficiency_status: payload.efficiency_status,
          efficiency_next_run: payload.efficiency_next_run,
          run_history_review_status: payload.run_history_review_status,
          recommended_api_servers: recommendedServers,
          errors_encountered: payload.errors_encountered ?? [],
          api_server_observations: payload.api_server_observations ?? [],
          learnings_recorded_at: new Date(),
        },
      }
    );
  }

  if (skipGraphiti) return { dirtyTriggered };

  const completedAt = subagentTask?.completed_at ?? new Date();
  const promptText = subagentTask
    ? (scheduledTask?.prompt ?? subagentTask.label ?? '')
    : (payload.prompt_text ?? '');
  const ingestStatus = finalStatus ?? (subagentTask?.status === 'done' ? 'done' : 'failed');

  ingestLearning({
    groupId: `learning_${auth.sub}`,
    promptText,
    requiredApiServers: scheduledTask?.required_api_servers ?? [],
    scheduledTaskId,
    subagentTaskId,
    personaId: subagentTask?.persona_id,
    status: ingestStatus,
    completedAt,
    durationMs: subagentTask?.completed_at && subagentTask?.started_at
      ? subagentTask.completed_at.getTime() - subagentTask.started_at.getTime()
      : undefined,
    turnsUsed: subagentTask?.turns_used,
    tokensTotal: subagentTask?.tokens?.total,
    errorCount: subagentTask?.error_count,
    apiServerSetStatus: dirtyTriggered ? 'dirty' : (scheduledTask?.api_server_set_status ?? 'clean'),
    apiServersUsed: payload.api_servers_used ?? [],
    errorsEncountered: payload.errors_encountered ?? [],
    apiServerObservations: payload.api_server_observations ?? [],
    efficiencyStatus: payload.efficiency_status,
    efficiencyNextRun: payload.efficiency_next_run,
    runHistoryReviewStatus: payload.run_history_review_status,
    recommendedApiServers: recommendedServers,
    preloadTaskPatternId: subagentTask?.preload_task_pattern_id,
    preloadEfficiencyHintId: subagentTask?.preload_efficiency_hint_id,
  });

  return { dirtyTriggered };
}
