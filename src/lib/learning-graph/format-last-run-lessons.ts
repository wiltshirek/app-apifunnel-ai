interface LastRunTask {
  task_id?: string;
  assessment?: {
    status?: string;
    summary?: string;
    outputs?: Array<{ type: string; description: string; detail?: string }>;
    errors?: Array<{ description: string; severity: string; attempted_resolution?: string }>;
    follow_up_needed?: boolean;
    follow_up_reason?: string;
    assumptions_made?: string[];
  };
  self_improvement_assessment?: {
    performance_delta?: string;
    evidence?: string;
    lessons_confirmed?: string[];
    lessons_invalidated?: string[];
    new_lessons?: string[];
  };
  performance_delta?: string;
  graphiti_ingested?: boolean;
  consecutive_stable_count?: number;
  graduation_eligible?: boolean;
  api_servers_used?: string[];
  efficiency_status?: string;
  efficiency_next_run?: string;
  errors_encountered?: Array<{
    api_server?: string;
    error_description: string;
    root_cause?: string;
    resolution: string;
  }>;
  api_server_observations?: Array<{ api_server: string; observation: string }>;
  recommended_api_servers?: Array<{ api_server: string; reason: string }>;
  started_at?: Date;
  completed_at?: Date;
  turns_used?: number;
  tokens?: { total?: number };
}

export function formatLastRunLessons(task: LastRunTask): string | null {
  const a = task.assessment;
  if (!a?.summary && !task.errors_encountered?.length && !task.api_server_observations?.length) {
    return null;
  }

  const lines: string[] = [];

  if (a) {
    lines.push(`**Outcome**: ${a.status ?? 'unknown'}`);
    if (a.summary) lines.push(`**Summary**: ${a.summary}`);

    if (a.errors?.length) {
      lines.push('', '**Issues encountered**:');
      for (const e of a.errors) {
        lines.push(`- [${e.severity}] ${e.description}${e.attempted_resolution ? ` → Resolution: ${e.attempted_resolution}` : ''}`);
      }
    }

    if (a.follow_up_needed && a.follow_up_reason) {
      lines.push(`\n**Follow-up needed**: ${a.follow_up_reason}`);
    }

    if (a.assumptions_made?.length) {
      lines.push('', '**Assumptions made**:');
      for (const assumption of a.assumptions_made) lines.push(`- ${assumption}`);
    }
  }

  if (task.errors_encountered?.length) {
    lines.push('', '**Error resolutions learned**:');
    for (const e of task.errors_encountered) {
      const server = e.api_server ? `[${e.api_server}] ` : '';
      lines.push(`- ${server}${e.error_description}`);
      if (e.root_cause) lines.push(`  Root cause: ${e.root_cause}`);
      lines.push(`  Resolution: ${e.resolution}`);
    }
  }

  if (task.api_server_observations?.length) {
    lines.push('', '**Server observations**:');
    for (const obs of task.api_server_observations) {
      lines.push(`- [${obs.api_server}] ${obs.observation}`);
    }
  }

  if (task.efficiency_status || task.efficiency_next_run) {
    lines.push('');
    if (task.efficiency_status === 'improvement_found') {
      lines.push(`**Efficiency**: Improvement found — ${task.efficiency_next_run ?? 'see details'}`);
    } else if (task.efficiency_status === 'no_further_tweaking_required') {
      lines.push('**Efficiency**: No further tweaking required.');
    }
  }

  if (task.recommended_api_servers?.length) {
    lines.push('', '**Recommended new servers**:');
    for (const r of task.recommended_api_servers) lines.push(`- ${r.api_server}: ${r.reason}`);
  }

  if (task.api_servers_used?.length) {
    lines.push('', `**Servers used**: ${task.api_servers_used.join(', ')}`);
  }

  if (task.turns_used || task.tokens?.total) {
    const parts: string[] = [];
    if (task.turns_used) parts.push(`${task.turns_used} turns`);
    if (task.tokens?.total) parts.push(`${task.tokens.total} tokens`);
    lines.push(`**Run stats**: ${parts.join(', ')}`);
  }

  const sia = task.self_improvement_assessment;
  if (sia?.performance_delta) {
    lines.push('', `**Prior run self-assessment**: ${sia.performance_delta}`);
    if (sia.evidence) lines.push(`**Evidence**: ${sia.evidence}`);
    if (sia.lessons_confirmed?.length) {
      lines.push('', '**Lessons confirmed last run**:');
      for (const l of sia.lessons_confirmed) lines.push(`- ${l}`);
    }
    if (sia.lessons_invalidated?.length) {
      lines.push('', '**Lessons invalidated last run**:');
      for (const l of sia.lessons_invalidated) lines.push(`- ${l}`);
    }
    if (sia.new_lessons?.length) {
      lines.push('', '**New lessons from last run**:');
      for (const l of sia.new_lessons) lines.push(`- ${l}`);
    }
  }

  if (typeof task.consecutive_stable_count === 'number' && task.consecutive_stable_count > 0) {
    lines.push('', `**Stability**: ${task.consecutive_stable_count} consecutive stable run(s).${task.graduation_eligible ? ' This task is graduation-eligible.' : ''}`);
  }

  return lines.join('\n');
}
