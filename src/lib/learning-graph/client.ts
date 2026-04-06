import { getGraphitiUrl, graphitiDisabledReason } from '../graphiti-runtime';

const PRELOAD_TIMEOUT_MS = 3000;
const TAG = '[LearningGraph]';

export interface ErrorEntry {
  api_server?: string;
  error_description: string;
  root_cause?: string;
  resolution: string;
}

export interface ServerObservation {
  api_server: string;
  observation: string;
}

export interface RecommendedAPIServer {
  api_server: string;
  reason: string;
}

export interface IngestLearningPayload {
  groupId: string;
  promptText: string;
  requiredApiServers: string[];
  scheduledTaskId?: string;
  subagentTaskId?: string;
  personaId?: string;
  status: 'done' | 'failed';
  completedAt: Date;
  durationMs?: number;
  turnsUsed?: number;
  tokensTotal?: number;
  errorCount?: number;
  apiServerSetStatus?: 'clean' | 'dirty';
  apiServersUsed: string[];
  errorsEncountered: ErrorEntry[];
  apiServerObservations: ServerObservation[];
  efficiencyStatus: 'improvement_found' | 'no_further_tweaking_required';
  efficiencyNextRun: string;
  runHistoryReviewStatus: 0 | 1;
  recommendedApiServers: RecommendedAPIServer[];
  preloadTaskPatternId?: string;
  preloadEfficiencyHintId?: string;
}

export interface PreloadResult {
  preload_block: string | null;
  task_pattern_matched: string | null;
  facts: string[];
}

export type LearningQueryType =
  | 'task_pattern_context'
  | 'api_server_history'
  | 'failure_mode_history'
  | 'efficiency_history'
  | 'output_contract_history';

export interface LearningQueryParams {
  groupId: string;
  queryType: LearningQueryType;
  promptText?: string;
  serverName?: string;
  limit?: number;
}

export interface LearningQueryResult {
  query_type: string;
  facts: string[];
}

export interface WalkAttempt {
  run_id?: string | null;
  status?: string | null;
  efficiency_status?: string | null;
  failure_modes_hit: string[];
  procedure_used?: string | null;
  efficiency_hint_applied?: string | null;
  turns_used?: number | null;
  tokens_total?: number | null;
}

export interface WalkLearningResult {
  task_pattern?: string | null;
  total_runs: number;
  attempts: WalkAttempt[];
  persistent_lessons: string[];
  recurring_failures: string[];
  active_procedure?: string | null;
  active_hints: string[];
  hint_supersession_chain: string[];
  improvement_trajectory?: 'improving' | 'regressing' | 'stable' | null;
}

export const VALID_LEARNING_QUERY_TYPES: readonly LearningQueryType[] = [
  'task_pattern_context',
  'api_server_history',
  'failure_mode_history',
  'efficiency_history',
  'output_contract_history',
] as const;

export function ingestLearning(payload: IngestLearningPayload): void {
  const graphitiUrl = getGraphitiUrl();
  if (!graphitiUrl) {
    console.log(`${TAG}   ingest SKIPPED (${graphitiDisabledReason()})`);
    return;
  }

  const url = `${graphitiUrl}/learning-graph/ingest`;
  const start = Date.now();

  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      group_id: payload.groupId,
      prompt_text: payload.promptText,
      required_api_servers: payload.requiredApiServers,
      scheduled_task_id: payload.scheduledTaskId,
      subagent_task_id: payload.subagentTaskId,
      persona_id: payload.personaId,
      status: payload.status,
      completed_at: payload.completedAt.toISOString(),
      duration_ms: payload.durationMs,
      turns_used: payload.turnsUsed,
      tokens_total: payload.tokensTotal,
      error_count: payload.errorCount,
      api_server_set_status: payload.apiServerSetStatus,
      api_servers_used: payload.apiServersUsed,
      errors_encountered: payload.errorsEncountered,
      api_server_observations: payload.apiServerObservations,
      efficiency_status: payload.efficiencyStatus,
      efficiency_next_run: payload.efficiencyNextRun,
      run_history_review_status: payload.runHistoryReviewStatus,
      recommended_api_servers: payload.recommendedApiServers,
      preload_task_pattern_id: payload.preloadTaskPatternId,
      preload_efficiency_hint_id: payload.preloadEfficiencyHintId,
    }),
  })
    .then((res) => {
      const ms = Date.now() - start;
      if (!res.ok) console.log(`${TAG}   ingest FAILED: HTTP ${res.status} (${ms}ms)`);
      else console.log(`${TAG}   ingest ACCEPTED: 202 (${ms}ms)`);
    })
    .catch((err) => {
      const ms = Date.now() - start;
      console.log(`${TAG}   ingest ERROR: ${err instanceof Error ? err.message : 'unknown'} (${ms}ms)`);
    });
}

export async function getPreload(
  groupId: string,
  promptText: string,
  requiredApiServers: string[],
): Promise<PreloadResult | null> {
  const graphitiUrl = getGraphitiUrl();
  if (!graphitiUrl) {
    console.log(`${TAG}   preload SKIPPED (${graphitiDisabledReason()})`);
    return null;
  }

  try {
    const res = await fetch(`${graphitiUrl}/learning-graph/preload`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_id: groupId, prompt_text: promptText, required_api_servers: requiredApiServers }),
      signal: AbortSignal.timeout(PRELOAD_TIMEOUT_MS),
    });
    if (!res.ok) return null;
    return await res.json() as PreloadResult;
  } catch {
    return null;
  }
}

export async function walkLearningGraph(params: {
  groupId: string;
  scheduledTaskId: string;
  limit?: number;
}): Promise<WalkLearningResult> {
  const empty: WalkLearningResult = {
    total_runs: 0, attempts: [], persistent_lessons: [],
    recurring_failures: [], active_hints: [], hint_supersession_chain: [],
  };
  const graphitiUrl = getGraphitiUrl();
  if (!graphitiUrl) return empty;

  try {
    const res = await fetch(`${graphitiUrl}/learning-graph/walk`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        group_id: params.groupId,
        scheduled_task_id: params.scheduledTaskId,
        walk_type: 'prior_attempts',
        include_assessments: true,
        limit: params.limit ?? 5,
      }),
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) return empty;
    return await res.json() as WalkLearningResult;
  } catch {
    return empty;
  }
}

export async function queryLearningGraph(params: LearningQueryParams): Promise<LearningQueryResult> {
  const empty: LearningQueryResult = { query_type: params.queryType, facts: [] };
  const graphitiUrl = getGraphitiUrl();
  if (!graphitiUrl) return empty;

  try {
    const res = await fetch(`${graphitiUrl}/learning-graph/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        group_id: params.groupId,
        query_type: params.queryType,
        prompt_text: params.promptText,
        server_name: params.serverName,
        limit: params.limit ?? 10,
      }),
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return empty;
    return await res.json() as LearningQueryResult;
  } catch {
    return empty;
  }
}
