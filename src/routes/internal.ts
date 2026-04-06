import { Hono } from 'hono';
import { connectDB } from '../lib/db';
import { ScheduledTask } from '../models/ScheduledTask';
import { SubagentTask } from '../models/SubagentTask';
import { calculateNextRun } from '../lib/utils/schedule';
import { getPreload } from '../lib/learning-graph/client';
import { formatLastRunLessons } from '../lib/learning-graph/format-last-run-lessons';
import { runSubagent } from '../lib/subagent-runner';
import { mintAppJwt } from '../lib/jwt';
import { generateThreadId } from '../lib/utils/schedule';

const CRON_SECRET = process.env.CRON_SECRET;

export const internalRouter = new Hono();

/**
 * POST /v1/internal/scheduler-tick
 *
 * Replaces the Vercel Cron trigger from the Next.js app.
 * Called by the in-process node-cron scheduler every minute, or externally
 * for on-demand kicks during testing. Protected by CRON_SECRET.
 *
 * Finds due ScheduledTask documents, skips dirty (pending approval) ones,
 * and dispatches clean ones to the subagent runner.
 */
internalRouter.post('/scheduler-tick', async (c) => {
  const authHeader = c.req.header('authorization');
  if (CRON_SECRET && authHeader !== `Bearer ${CRON_SECRET}`) {
    return c.json({ error: 'Unauthorized' }, 401);
  }

  await connectDB();

  const now = new Date();
  const dueTasks = await ScheduledTask.find({
    enabled: true,
    deleted_at: { $exists: false },
    next_run_at: { $lte: now },
    $or: [
      { api_server_set_status: { $exists: false } },
      { api_server_set_status: 'clean' },
    ],
  }).limit(20);

  if (dueTasks.length === 0) {
    return c.json({ dispatched: 0, skipped_dirty: 0 });
  }

  const base_url = process.env.APP_BASE_URL || 'http://localhost:4000';
  let dispatched = 0;

  for (const task of dueTasks) {
    const claimed = await ScheduledTask.findOneAndUpdate(
      {
        scheduled_task_id: task.scheduled_task_id,
        next_run_at: task.next_run_at,
        enabled: true,
        $or: [
          { api_server_set_status: { $exists: false } },
          { api_server_set_status: 'clean' },
        ],
      },
      { $set: { next_run_at: calculateNextRun(task.schedule, task.timezone || 'UTC') } },
      { new: false }
    );

    if (!claimed) continue;

    const preload = await getPreload(
      `learning_${task.user_id}`,
      task.prompt,
      task.required_api_servers ?? []
    ).catch(() => null);

    const lastRun = await SubagentTask.findOne(
      { scheduled_task_id: task.scheduled_task_id, status: { $in: ['done', 'failed'] } },
      { assessment: 1, api_servers_used: 1, efficiency_status: 1, efficiency_next_run: 1,
        errors_encountered: 1, api_server_observations: 1, recommended_api_servers: 1,
        turns_used: 1, tokens: 1, started_at: 1, completed_at: 1, task_id: 1 },
    ).sort({ completed_at: -1 }).lean();

    const lastRunLessonsBlock = lastRun ? formatLastRunLessons(lastRun as any) ?? undefined : undefined;

    const task_id = `sa_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`;
    let augmentedPrompt = task.prompt;
    augmentedPrompt += `\n\n[scheduler_context: scheduled_task_id=${task.scheduled_task_id}, subagent_task_id=${task_id}]`;

    const jwt_token = mintAppJwt(
      {
        sub: task.user_id, email: '', instance_id: task.instance_id, tier: 'enterprise', role: 'user',
        user_id: task.user_id, scheduled_task_id: task.scheduled_task_id, subagent_task_id: task_id,
      },
      task.required_api_servers ?? [],
      {},
    );

    const thread_id = generateThreadId();
    const label = task.label || task.prompt.slice(0, 60);

    await SubagentTask.create({
      task_id, thread_id, user_id: task.user_id, label, status: 'running',
      message: 'Scheduled run started.', persona_id: task.persona_id,
      max_turns: task.max_turns ?? 30, started_at: new Date(),
      scheduled_task_id: task.scheduled_task_id,
      preload_task_pattern_id: preload?.task_pattern_matched ?? undefined,
    });

    // Fire and forget — Promise is intentionally not awaited for response
    (async () => {
      try {
        await runSubagent({
          task_id, thread_id, user_id: task.user_id, message: augmentedPrompt, label,
          persona_id: task.persona_id, max_turns: task.max_turns ?? 30, jwt_token, base_url,
          execution_mode: 'autonomous',
          learning_preload_block: preload?.preload_block ?? undefined,
          last_run_lessons_block: lastRunLessonsBlock,
        });

        const finishedTask = await SubagentTask.findOne({ task_id }).lean();
        if (finishedTask) {
          await ScheduledTask.findOneAndUpdate(
            { scheduled_task_id: task.scheduled_task_id },
            {
              $set: {
                last_executed_at: new Date(),
                last_run: {
                  status: (finishedTask as any).status === 'done' ? 'done' : 'failed',
                  output: (finishedTask as any).result?.slice(0, 2000),
                  error: (finishedTask as any).error,
                  task_id,
                  started_at: (finishedTask as any).started_at,
                  completed_at: (finishedTask as any).completed_at ?? new Date(),
                  duration_ms: (finishedTask as any).completed_at
                    ? (finishedTask as any).completed_at.getTime() - (finishedTask as any).started_at.getTime()
                    : undefined,
                },
              },
              $inc: { run_count: 1 },
            }
          );
        }
      } catch (err) {
        console.error(`[SchedulerTick] run failed for ${task.scheduled_task_id}:`, err);
      }
    })();

    dispatched++;
  }

  return c.json({ dispatched, skipped_dirty: dueTasks.length - dispatched });
});
