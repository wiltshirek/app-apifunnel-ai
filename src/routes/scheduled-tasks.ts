import { Hono } from 'hono';
import { connectDB } from '../lib/db';
import { getAuthFromRequest } from '../lib/jwt';
import { ScheduledTask } from '../models/ScheduledTask';
import { SubagentTask } from '../models/SubagentTask';
import { ConversationThread } from '../models/ConversationThread';
import { calculateNextRun, isValidCron } from '../lib/utils/schedule';

export const scheduledTasksRouter = new Hono();

// GET /v1/scheduled-tasks
scheduledTasksRouter.get('/', async (c) => {
  try {
    await connectDB();
    const auth = getAuthFromRequest(c.req.raw);
    if (!auth) return c.json({ error: 'Unauthorized' }, 401);

    const enabledFilter = c.req.query('enabled');
    const filter: Record<string, unknown> = { user_id: auth.sub, deleted_at: { $exists: false } };
    if (enabledFilter === 'true') filter.enabled = true;
    if (enabledFilter === 'false') filter.enabled = false;

    const tasks = await ScheduledTask.find(filter).sort({ created_at: -1 });
    return c.json({ scheduled_tasks: tasks });
  } catch (error) {
    console.error('Failed to list scheduled tasks:', error);
    return c.json({ error: 'Failed to list scheduled tasks' }, 500);
  }
});

// POST /v1/scheduled-tasks
scheduledTasksRouter.post('/', async (c) => {
  try {
    await connectDB();
    const auth = getAuthFromRequest(c.req.raw);
    if (!auth) return c.json({ error: 'Unauthorized' }, 401);

    const body = await c.req.json();

    if (!body.prompt || typeof body.prompt !== 'string' || !body.prompt.trim()) {
      return c.json({ error: 'validation_error', message: "Field 'prompt' is required and must be a non-empty string." }, 400);
    }
    if (!body.schedule || !isValidCron(body.schedule)) {
      return c.json({ error: 'validation_error', message: "Invalid or missing cron expression in field 'schedule'." }, 400);
    }

    const timezone = body.timezone || 'UTC';
    const scheduledTaskId = `scht_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`;

    const task = await ScheduledTask.create({
      scheduled_task_id: scheduledTaskId,
      user_id: auth.sub,
      instance_id: auth.instance_id,
      prompt: body.prompt.trim(),
      persona_id: body.persona_id || undefined,
      required_api_servers: Array.isArray(body.required_api_servers) ? body.required_api_servers : undefined,
      schedule: body.schedule,
      timezone,
      enabled: body.enabled !== undefined ? body.enabled : true,
      label: body.label || body.prompt.trim().slice(0, 60),
      description: body.description || undefined,
      max_turns: body.max_turns ? Math.min(Math.max(body.max_turns, 1), 50) : 30,
      next_run_at: calculateNextRun(body.schedule, timezone),
      run_count: 0,
    });

    return c.json(task, 201);
  } catch (error) {
    console.error('Failed to create scheduled task:', error);
    return c.json({ error: 'Failed to create scheduled task' }, 500);
  }
});

// GET /v1/scheduled-tasks/:id
scheduledTasksRouter.get('/:id', async (c) => {
  try {
    await connectDB();
    const auth = getAuthFromRequest(c.req.raw);
    if (!auth) return c.json({ error: 'Unauthorized' }, 401);

    const id = c.req.param('id');
    const task = await ScheduledTask.findOne({ scheduled_task_id: id, user_id: auth.sub, deleted_at: { $exists: false } });
    if (!task) return c.json({ error: 'not_found', message: `Scheduled task ${id} not found.` }, 404);
    return c.json(task);
  } catch (error) {
    console.error('Failed to fetch scheduled task:', error);
    return c.json({ error: 'Failed to fetch scheduled task' }, 500);
  }
});

// PUT /v1/scheduled-tasks/:id
scheduledTasksRouter.put('/:id', async (c) => {
  try {
    await connectDB();
    const auth = getAuthFromRequest(c.req.raw);
    if (!auth) return c.json({ error: 'Unauthorized' }, 401);

    const id = c.req.param('id');
    const body = await c.req.json();

    const task = await ScheduledTask.findOne({ scheduled_task_id: id, user_id: auth.sub, deleted_at: { $exists: false } });
    if (!task) return c.json({ error: 'not_found', message: `Scheduled task ${id} not found.` }, 404);

    const approveServers: string[] = body.approve_servers ?? [];
    const rejectServers: string[] = body.reject_servers ?? [];
    delete body.approve_servers;
    delete body.reject_servers;

    if (approveServers.length > 0 || rejectServers.length > 0) {
      const currentRequired: string[] = (task as any).required_api_servers ?? [];
      const currentPending: Array<{ api_server: string }> = (task as any).pending_api_server_recommendations ?? [];
      const newRequired = [
        ...currentRequired.filter((s: string) => !rejectServers.includes(s)),
        ...approveServers.filter((s: string) => !currentRequired.includes(s)),
      ];
      const newPending = currentPending.filter(
        (rec: { api_server: string }) => !approveServers.includes(rec.api_server) && !rejectServers.includes(rec.api_server)
      );
      body.required_api_servers = newRequired;
      body.pending_api_server_recommendations = newPending;
      body.api_server_set_status = newPending.length > 0 ? 'dirty' : 'clean';
      if (newPending.length === 0) delete body.blocked_reason;
    }

    delete body.scheduled_task_id; delete body.user_id; delete body.instance_id;
    delete body.run_count; delete body.last_run; delete body.last_executed_at;

    if (body.schedule !== undefined && !isValidCron(body.schedule)) {
      return c.json({ error: 'validation_error', message: 'Invalid cron expression.' }, 400);
    }
    if (body.prompt !== undefined) {
      if (typeof body.prompt !== 'string' || !body.prompt.trim()) {
        return c.json({ error: 'validation_error', message: "Field 'prompt' must be a non-empty string." }, 400);
      }
      body.prompt = body.prompt.trim();
    }
    if (body.max_turns !== undefined) body.max_turns = Math.min(Math.max(body.max_turns, 1), 50);
    if (body.schedule !== undefined || body.timezone !== undefined) {
      const cronExpr = body.schedule || (task as any).schedule;
      const tz = body.timezone !== undefined ? body.timezone : ((task as any).timezone || 'UTC');
      body.next_run_at = calculateNextRun(cronExpr, tz);
    }

    const updated = await ScheduledTask.findOneAndUpdate(
      { scheduled_task_id: id, user_id: auth.sub }, { $set: body }, { new: true }
    );
    return c.json(updated);
  } catch (error) {
    console.error('Failed to update scheduled task:', error);
    return c.json({ error: 'Failed to update scheduled task' }, 500);
  }
});

// PATCH aliases PUT
scheduledTasksRouter.patch('/:id', async (c) => {
  const newC = { ...c };
  return scheduledTasksRouter.fetch(
    new Request(c.req.url, { method: 'PUT', headers: c.req.raw.headers, body: c.req.raw.body }),
    c.env
  );
});

// DELETE /v1/scheduled-tasks/:id
scheduledTasksRouter.delete('/:id', async (c) => {
  try {
    await connectDB();
    const auth = getAuthFromRequest(c.req.raw);
    if (!auth) return c.json({ error: 'Unauthorized' }, 401);

    const id = c.req.param('id');
    const task = await ScheduledTask.findOne({ scheduled_task_id: id, user_id: auth.sub, deleted_at: { $exists: false } });
    if (!task) return c.json({ error: 'not_found', message: `Scheduled task ${id} not found.` }, 404);
    await ScheduledTask.findOneAndUpdate({ scheduled_task_id: id, user_id: auth.sub }, { $set: { deleted_at: new Date(), enabled: false } });
    return c.json({ scheduled_task_id: id, deleted: true });
  } catch (error) {
    console.error('Failed to delete scheduled task:', error);
    return c.json({ error: 'Failed to delete scheduled task' }, 500);
  }
});

// POST /v1/scheduled-tasks/:id/run-now
scheduledTasksRouter.post('/:id/run-now', async (c) => {
  try {
    await connectDB();
    const auth = getAuthFromRequest(c.req.raw);
    if (!auth) return c.json({ error: 'Unauthorized' }, 401);

    const id = c.req.param('id');
    const task = await ScheduledTask.findOne({ scheduled_task_id: id, user_id: auth.sub, deleted_at: { $exists: false } });
    if (!task) return c.json({ error: 'not_found', message: `Scheduled task ${id} not found.` }, 404);
    await ScheduledTask.findOneAndUpdate({ scheduled_task_id: id, user_id: auth.sub }, { $set: { next_run_at: new Date() } });
    return c.json({ scheduled_task_id: id, queued: true });
  } catch (error) {
    return c.json({ error: 'Failed to queue task' }, 500);
  }
});

// GET /v1/scheduled-tasks/:id/runs
scheduledTasksRouter.get('/:id/runs', async (c) => {
  try {
    await connectDB();
    const auth = getAuthFromRequest(c.req.raw);
    if (!auth) return c.json({ error: 'Unauthorized' }, 401);

    const id = c.req.param('id');
    const task = await ScheduledTask.findOne({ scheduled_task_id: id, user_id: auth.sub, deleted_at: { $exists: false } }).lean();
    if (!task) return c.json({ error: 'not_found', message: `Scheduled task ${id} not found.` }, 404);

    const cursor = c.req.query('cursor') ?? null;
    const limit = Math.min(Math.max(parseInt(c.req.query('limit') ?? '20', 10), 1), 100);
    const statusFilter = c.req.query('status') ?? null;
    const includeMessages = c.req.query('include')?.split(',').includes('messages') ?? false;

    const filter: Record<string, unknown> = { scheduled_task_id: id, user_id: auth.sub };
    if (statusFilter) filter.status = statusFilter === 'done' ? 'done' : 'failed';
    if (cursor) {
      const cursorTask = await SubagentTask.findOne({ task_id: cursor }).lean();
      if (cursorTask) filter.started_at = { $lt: (cursorTask as any).started_at };
    }

    const runs = await SubagentTask.find(filter)
      .sort({ started_at: -1 }).limit(limit + 1)
      .select('task_id thread_id scheduled_task_id status started_at completed_at tokens turns_used max_turns api_servers_used efficiency_status efficiency_next_run run_history_review_status recommended_api_servers error_count error learnings_recorded_at result assessment')
      .lean();

    const hasMore = runs.length > limit;
    const page = runs.slice(0, limit);

    let messagesByThread = new Map<string, unknown[]>();
    if (includeMessages && page.length > 0) {
      const threadIds = [...new Set(page.map((r: any) => r.thread_id).filter(Boolean))] as string[];
      if (threadIds.length > 0) {
        const threads = await ConversationThread.find({ thread_id: { $in: threadIds }, user_id: auth.sub }).select('thread_id messages').lean();
        messagesByThread = new Map(threads.map((t: any) => [t.thread_id, t.messages ?? []]));
      }
    }

    return c.json({
      runs: page.map((r: any) => {
        const row: Record<string, unknown> = {
          run_id: r.task_id, scheduled_task_id: r.scheduled_task_id, status: r.status,
          started_at: r.started_at, completed_at: r.completed_at,
          duration_ms: r.completed_at && r.started_at ? new Date(r.completed_at).getTime() - new Date(r.started_at).getTime() : null,
          tokens: r.tokens ?? null, turns_used: r.turns_used ?? null, max_turns: r.max_turns,
          api_servers_used: r.api_servers_used ?? [], efficiency_status: r.efficiency_status ?? null,
          efficiency_next_run: r.efficiency_next_run ?? null, run_history_review_status: r.run_history_review_status ?? null,
          recommended_api_servers: r.recommended_api_servers ?? [], error_count: r.error_count ?? 0,
          error: r.error ?? null, learnings_recorded_at: r.learnings_recorded_at ?? null,
          result: r.result ?? null, assessment: r.assessment ?? null,
        };
        if (includeMessages) row.messages = messagesByThread.get(r.thread_id) ?? [];
        return row;
      }),
      has_more: hasMore,
      next_cursor: hasMore ? (page[page.length - 1] as any).task_id : null,
    });
  } catch (error) {
    console.error('GET /v1/scheduled-tasks/:id/runs error:', error);
    return c.json({ error: 'Failed to fetch run history' }, 500);
  }
});

// GET /v1/scheduled-tasks/:id/runs/summary
scheduledTasksRouter.get('/:id/runs/summary', async (c) => {
  try {
    await connectDB();
    const auth = getAuthFromRequest(c.req.raw);
    if (!auth) return c.json({ error: 'Unauthorized' }, 401);

    const id = c.req.param('id');
    const task = await ScheduledTask.findOne({ scheduled_task_id: id, user_id: auth.sub, deleted_at: { $exists: false } }).lean();
    if (!task) return c.json({ error: 'not_found', message: `Scheduled task ${id} not found.` }, 404);

    const runs = await SubagentTask.find({ scheduled_task_id: id, user_id: auth.sub })
      .sort({ started_at: -1 }).limit(100)
      .select('task_id status started_at completed_at tokens turns_used error_count efficiency_status efficiency_next_run run_history_review_status api_servers_used recommended_api_servers error')
      .lean();

    if (runs.length === 0) {
      return c.json({
        scheduled_task_id: id, total_runs: 0, successful_runs: 0, failed_runs: 0,
        median_duration_ms: null, median_tokens: null, median_turns: null,
        current_api_servers: (task as any).required_api_servers ?? [],
        api_server_set_status: (task as any).api_server_set_status ?? 'clean',
        blocked_reason: (task as any).blocked_reason ?? null,
        pending_api_recommendations: (task as any).pending_api_server_recommendations ?? [],
        most_recent_efficiency_hint: null, most_common_errors: [], runs_without_learnings: 0,
      });
    }

    const successful = runs.filter((r: any) => r.status === 'done');
    const failed = runs.filter((r: any) => r.status === 'failed');
    const durations = runs.filter((r: any) => r.completed_at && r.started_at)
      .map((r: any) => new Date(r.completed_at).getTime() - new Date(r.started_at).getTime()).sort((a, b) => a - b);
    const tokens = runs.filter((r: any) => r.tokens?.total).map((r: any) => r.tokens.total).sort((a: number, b: number) => a - b);
    const turns = runs.filter((r: any) => r.turns_used).map((r: any) => r.turns_used!).sort((a: number, b: number) => a - b);
    const mostRecentHint = runs.filter((r: any) => r.efficiency_next_run).map((r: any) => ({
      hint: r.efficiency_next_run, status: r.efficiency_status, run_id: r.task_id, run_history_review_status: r.run_history_review_status,
    }))[0] ?? null;

    const errorCounts: Record<string, number> = {};
    for (const run of failed) {
      if ((run as any).error) { const k = (run as any).error.slice(0, 80); errorCounts[k] = (errorCounts[k] ?? 0) + 1; }
    }
    const mostCommonErrors = Object.entries(errorCounts).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([error, count]) => ({ error, count }));
    const runsWithoutLearnings = runs.filter((r: any) => r.status === 'done' && !r.efficiency_status).length;

    return c.json({
      scheduled_task_id: id, total_runs: runs.length, successful_runs: successful.length, failed_runs: failed.length,
      median_duration_ms: median(durations), median_tokens: median(tokens), median_turns: median(turns),
      current_api_servers: (task as any).required_api_servers ?? [],
      api_server_set_status: (task as any).api_server_set_status ?? 'clean',
      blocked_reason: (task as any).blocked_reason ?? null,
      pending_api_recommendations: (task as any).pending_api_server_recommendations ?? [],
      most_recent_efficiency_hint: mostRecentHint, most_common_errors: mostCommonErrors,
      runs_without_learnings: runsWithoutLearnings,
    });
  } catch (error) {
    return c.json({ error: 'Failed to fetch run summary' }, 500);
  }
});

function median(sorted: number[]): number | null {
  if (sorted.length === 0) return null;
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? Math.round((sorted[mid - 1] + sorted[mid]) / 2) : sorted[mid];
}
