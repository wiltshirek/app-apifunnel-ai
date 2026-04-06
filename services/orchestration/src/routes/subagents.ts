import { Hono } from 'hono';
import { connectDB } from '../lib/db';
import { authenticateInternalRequest } from '../lib/auth-internal';
import { mintAppJwt } from '../lib/jwt';
import { SubagentTask } from '../models/SubagentTask';
import { ConversationThread } from '../models/ConversationThread';
import { getAdminFirestore, firestoreConfigured } from '../lib/firebase-admin';
import { runSubagent } from '../lib/subagent-runner';
import { getPreload } from '../lib/learning-graph/client';
import { generateThreadId } from '../lib/utils/schedule';

const MAX_CONCURRENT_PER_USER = 5;
const DEFAULT_MAX_TURNS = 30;
const HARD_MAX_TURNS = 50;
const DEFAULT_LIMIT = 50;
const MAX_LIMIT = 200;

// Minimal user settings cache (this server doesn't have MongoDB UserApiSettings —
// subagent dispatch carries the JWT from the calling platform which already has
// enabled_servers baked in, so we use the auth payload directly)
async function getEnabledServersFromAuth(auth: any): Promise<{ enabledServers: string[]; apiSettings: Record<string, any> }> {
  return {
    enabledServers: auth.enabled_servers ?? [],
    apiSettings: auth.api_settings ?? {},
  };
}

export const subagentsRouter = new Hono();

// POST /v1/subagents — Launch a new subagent task
subagentsRouter.post('/', async (c) => {
  await connectDB();
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const body = await c.req.json();
  if (!body.message || typeof body.message !== 'string') {
    return c.json({ error: 'A message is required to start a task.' }, 400);
  }

  const activeCount = await SubagentTask.countDocuments({ user_id: auth.sub, status: 'running' });
  if (activeCount >= MAX_CONCURRENT_PER_USER) {
    return c.json({ error: `You already have ${MAX_CONCURRENT_PER_USER} tasks running.` }, 429);
  }

  const task_id = `sa_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`;
  const thread_id = generateThreadId();
  const label = body.label || body.message.slice(0, 60);
  const persona_id = body.persona_id || 'cowork-enterprise';
  const max_turns = Math.min(body.max_turns ?? DEFAULT_MAX_TURNS, HARD_MAX_TURNS);

  await SubagentTask.create({
    task_id, thread_id, user_id: auth.sub, label,
    status: 'running', message: "Task started. I'll notify you when it's done.",
    persona_id, max_turns, started_at: new Date(),
  });

  if (firestoreConfigured) {
    try {
      const db = getAdminFirestore();
      await db.doc(`subagent_tasks/${auth.sub}/active/${task_id}`).set({
        task_id, thread_id, label, status: 'running',
        progress: { step: 0, note: 'Starting...', updated_at: new Date().toISOString() },
        started_at: new Date().toISOString(), completed_at: null,
      });
    } catch (err) {
      console.error('[POST /v1/subagents] Firestore seed failed:', err);
    }
  }

  const base_url = process.env.APP_BASE_URL || 'http://localhost:4000';
  const { enabledServers, apiSettings } = await getEnabledServersFromAuth(auth);
  const jwt_token = mintAppJwt({ ...auth, subagent_task_id: task_id }, enabledServers, apiSettings);

  const preload = await getPreload(`learning_${auth.sub}`, body.message, enabledServers).catch(() => null);

  // Fire and forget
  runSubagent({
    task_id, thread_id, user_id: auth.sub, message: body.message, label, persona_id, max_turns,
    jwt_token, base_url, execution_mode: body.execution_mode,
    learning_preload_block: preload?.preload_block ?? undefined,
  }).catch(err => console.error(`[subagents] background runSubagent error:`, err));

  return c.json({ task_id, status: 'running', message: "Task started. I'll notify you when it's done.", label, thread_id });
});

// GET /v1/subagents — List tasks for authenticated user
subagentsRouter.get('/', async (c) => {
  await connectDB();
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const tasks = await SubagentTask.find({ user_id: auth.sub })
    .sort({ started_at: -1 })
    .limit(50)
    .select('task_id thread_id label status message progress started_at completed_at')
    .lean();

  return c.json({
    tasks: tasks.map((t: any) => ({
      task_id: t.task_id, status: t.status, label: t.label, message: t.message,
      progress: t.progress, started_at: t.started_at, completed_at: t.completed_at,
    })),
  });
});

// GET /v1/subagents/:id — Get task status
subagentsRouter.get('/:id', async (c) => {
  await connectDB();
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const task = await SubagentTask.findOne({ task_id: id, user_id: auth.sub })
    .select('task_id thread_id label status message progress tokens started_at completed_at')
    .lean();

  if (!task) return c.json({ error: 'Task not found.' }, 404);

  return c.json({
    task_id: (task as any).task_id, status: (task as any).status, label: (task as any).label,
    message: (task as any).message, progress: (task as any).progress, tokens: (task as any).tokens,
    started_at: (task as any).started_at, completed_at: (task as any).completed_at,
  });
});

// DELETE /v1/subagents/:id — Cancel a task
subagentsRouter.delete('/:id', async (c) => {
  await connectDB();
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const task = await SubagentTask.findOneAndUpdate(
    { task_id: id, user_id: auth.sub, status: 'running' },
    { $set: { status: 'cancelled', message: 'Task stopped.', completed_at: new Date() } },
    { new: true }
  );

  if (!task) return c.json({ error: 'Task not found or not running.' }, 404);

  if (firestoreConfigured) {
    try {
      const db = getAdminFirestore();
      await db.doc(`subagent_tasks/${auth.sub}/active/${id}`)
        .set({ status: 'cancelled', completed_at: new Date().toISOString() }, { merge: true });
    } catch { /* non-fatal */ }
  }

  return c.json({ task_id: (task as any).task_id, status: 'cancelled', message: 'Task stopped.' });
});

// GET /v1/subagents/:id/response — Full subagent outputs (paginated thread)
subagentsRouter.get('/:id/response', async (c) => {
  await connectDB();
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const threadIdParam = c.req.query('thread_id') ?? null;
  const includeLessonsLearned = c.req.query('include_lessons_learned') === 'true';
  const offset = Math.max(0, parseInt(c.req.query('offset') ?? '0', 10) || 0);
  const limit = Math.min(MAX_LIMIT, Math.max(1, parseInt(c.req.query('limit') ?? String(DEFAULT_LIMIT), 10) || DEFAULT_LIMIT));

  const task = await resolveTask(id, threadIdParam, auth.sub);
  if (!task) return c.json({ error: 'Task not found.' }, 404);

  if ((task as any).status === 'running') {
    return c.json({
      error: 'not_completed',
      message: `Subagent is still running. Poll GET /v1/subagents/${(task as any).task_id} for progress.`,
      task_id: (task as any).task_id, status: (task as any).status, progress: (task as any).progress,
    }, 409);
  }

  let messages: any[] = [];
  let total = 0;

  if ((task as any).thread_id) {
    const countResult = await ConversationThread.aggregate([
      { $match: { thread_id: (task as any).thread_id, user_id: auth.sub } },
      { $project: { count: { $size: { $ifNull: ['$messages', []] } } } },
    ]);
    total = countResult[0]?.count ?? 0;

    if (total > 0) {
      const thread = await ConversationThread.findOne(
        { thread_id: (task as any).thread_id, user_id: auth.sub },
        { messages: { $slice: [offset, limit] } },
      ).lean();
      messages = (thread as any)?.messages ?? [];
    }
  }

  const finalResponse = await extractFinalResponse(task, auth.sub);
  const now = new Date();

  if (!(task as any).response_consumed) {
    await SubagentTask.updateOne(
      { task_id: (task as any).task_id, response_consumed: { $ne: true } },
      { $set: { response_consumed: true, delivered_at: now } },
    );
  }

  return c.json({
    task_id: (task as any).task_id,
    thread_id: (task as any).thread_id,
    label: (task as any).label,
    status: (task as any).status === 'done' ? 'completed' : (task as any).status,
    response: finalResponse,
    response_consumed: (task as any).response_consumed || true,
    assessment: (task as any).assessment ?? null,
    lessons_learned: buildLessonsLearned(task, includeLessonsLearned),
    tokens: (task as any).tokens ?? null,
    turns_used: (task as any).turns_used ?? null,
    max_turns: (task as any).max_turns,
    created_at: (task as any).started_at,
    completed_at: (task as any).completed_at,
    delivered_at: (task as any).delivered_at ?? now,
    messages,
    pagination: { offset, limit, total, has_more: offset + limit < total },
  });
});

// POST /v1/subagents/:id/message — Send follow-up to a completed task
subagentsRouter.post('/:id/message', async (c) => {
  await connectDB();
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');
  const body = await c.req.json();

  if (!body.message || typeof body.message !== 'string') {
    return c.json({ error: 'A message is required.' }, 400);
  }

  const task = await SubagentTask.findOne({ task_id: id, user_id: auth.sub });
  if (!task) return c.json({ error: 'Task not found.' }, 404);

  if ((task as any).status === 'running') {
    return c.json({ error: 'Task is still running. Wait for it to finish before sending a follow-up.' }, 409);
  }

  await SubagentTask.updateOne(
    { task_id: id },
    { $set: { status: 'running', message: 'Got it, working on that now.', result: undefined, completed_at: undefined,
        progress: { step: 0, note: 'Processing follow-up...', updated_at: new Date() } } }
  );

  const base_url = process.env.APP_BASE_URL || 'http://localhost:4000';
  const { enabledServers, apiSettings } = await getEnabledServersFromAuth(auth);
  const jwt_token = mintAppJwt(
    { ...auth, subagent_task_id: id, ...((task as any).scheduled_task_id && { scheduled_task_id: (task as any).scheduled_task_id }) },
    enabledServers, apiSettings,
  );

  runSubagent({
    task_id: id, thread_id: (task as any).thread_id, user_id: auth.sub, message: body.message,
    persona_id: (task as any).persona_id, max_turns: (task as any).max_turns, jwt_token, base_url,
  }).catch(err => console.error(`[subagents] background follow-up error:`, err));

  return c.json({ task_id: id, status: 'running', message: 'Got it, working on that now.' });
});

// ── helpers ──────────────────────────────────────────────────────────────────

async function resolveTask(id: string, threadIdParam: string | null, userId: string) {
  if (threadIdParam) return SubagentTask.findOne({ thread_id: threadIdParam, user_id: userId }).lean();
  const byTaskId = await SubagentTask.findOne({ task_id: id, user_id: userId }).lean();
  if (byTaskId) return byTaskId;
  return SubagentTask.findOne({ thread_id: id, user_id: userId }).lean();
}

async function extractFinalResponse(task: any, userId: string): Promise<string | null> {
  if (!task.thread_id) return task.result ?? null;
  const thread = await ConversationThread.findOne(
    { thread_id: task.thread_id, user_id: userId },
    { messages: { $slice: -10 } },
  ).lean();
  const msgs = (thread as any)?.messages;
  if (!msgs || !Array.isArray(msgs)) return task.result ?? null;
  for (let i = msgs.length - 1; i >= 0; i--) {
    const msg = msgs[i] as any;
    if (msg.role !== 'assistant' || !msg.tool_calls) continue;
    for (const tc of msg.tool_calls) {
      const fnName = tc.function?.name ?? tc.name;
      if (fnName !== 'deliver_final_response') continue;
      try {
        const args = typeof tc.function?.arguments === 'string' ? JSON.parse(tc.function.arguments) : tc.function?.arguments ?? tc.arguments;
        if (args?.content) return args.content;
      } catch { /* skip malformed */ }
    }
  }
  return task.result ?? null;
}

const SNIPPET = 100;
function truncate(v: string | undefined | null): string | null {
  if (!v) return null;
  return v.length > SNIPPET ? v.slice(0, SNIPPET) + '…' : v;
}

function truncateArray<T extends Record<string, any>>(arr: T[] | undefined | null, fields: string[]): T[] | null {
  if (!arr?.length) return null;
  return arr.map(item => { const o: any = { ...item }; for (const f of fields) if (typeof o[f] === 'string') o[f] = truncate(o[f]); return o; });
}

function buildLessonsLearned(task: any, full: boolean) {
  if (!task.learnings_recorded_at && !task.assessment?.summary) return null;
  if (full) {
    return {
      assessment_summary: task.assessment?.summary ?? null, assessment_outputs: task.assessment?.outputs ?? null,
      assessment_errors: task.assessment?.errors ?? null, assumptions_made: task.assessment?.assumptions_made ?? null,
      follow_up_needed: task.assessment?.follow_up_needed ?? null, follow_up_reason: task.assessment?.follow_up_reason ?? null,
      api_servers_used: task.api_servers_used ?? null, efficiency_status: task.efficiency_status ?? null,
      efficiency_next_run: task.efficiency_next_run ?? null, errors_encountered: task.errors_encountered ?? null,
      api_server_observations: task.api_server_observations ?? null, recommended_api_servers: task.recommended_api_servers ?? null,
      run_history_review_status: task.run_history_review_status ?? null, learnings_recorded_at: task.learnings_recorded_at ?? null,
    };
  }
  return {
    assessment_summary: truncate(task.assessment?.summary), assessment_outputs: truncateArray(task.assessment?.outputs, ['description', 'detail']),
    assessment_errors: truncateArray(task.assessment?.errors, ['description', 'attempted_resolution']),
    assumptions_made: task.assessment?.assumptions_made?.map((s: string) => truncate(s)) ?? null,
    follow_up_needed: task.assessment?.follow_up_needed ?? null, follow_up_reason: truncate(task.assessment?.follow_up_reason),
    api_servers_used: task.api_servers_used ?? null, efficiency_status: task.efficiency_status ?? null,
    efficiency_next_run: truncate(task.efficiency_next_run), errors_encountered: truncateArray(task.errors_encountered, ['error_description', 'root_cause', 'resolution']),
    api_server_observations: truncateArray(task.api_server_observations, ['observation']),
    recommended_api_servers: truncateArray(task.recommended_api_servers, ['reason']),
    run_history_review_status: task.run_history_review_status ?? null, learnings_recorded_at: task.learnings_recorded_at ?? null,
  };
}
