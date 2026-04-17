/**
 * Subagent Runner — fire-and-forget SSE consumer.
 *
 * Calls POST /api/chat on the web app (APP_BASE_URL) with the subagent's message,
 * streams the SSE response, writes progress to Firestore (throttled),
 * and writes the final result to MongoDB + pushes a notification.
 */
import { SubagentTask } from '../models/SubagentTask';
import { ConversationThread } from '../models/ConversationThread';
import { getAdminFirestore, firestoreConfigured } from './firebase-admin';
import { pushNotification } from './notifications/push';
import { connectDB } from './db';

const PROGRESS_THROTTLE_MS = 2000;
const LOG_FLUSH_MS = 5000;

export interface RunSubagentParams {
  task_id: string;
  thread_id: string;
  user_id: string;
  message: string;
  label?: string;
  persona_id?: string;
  max_turns: number;
  jwt_token: string;
  base_url: string;
  execution_mode?: string;
  learning_preload_block?: string;
  last_run_lessons_block?: string;
}

export async function runSubagent(params: RunSubagentParams): Promise<void> {
  const {
    task_id, thread_id, user_id, message, label,
    persona_id, max_turns, jwt_token, base_url, execution_mode,
    learning_preload_block, last_run_lessons_block,
  } = params;

  const pending: { ts: string; msg: string }[] = [];
  let lastLogFlush = 0;

  function log(msg: string) {
    pending.push({ ts: new Date().toISOString(), msg });
    console.log(`[SubagentRunner:${task_id}] ${msg}`);
  }

  async function flushLog(force = false) {
    if (pending.length === 0) return;
    const now = Date.now();
    if (!force && now - lastLogFlush < LOG_FLUSH_MS) return;
    const batch = pending.splice(0);
    lastLogFlush = now;
    try {
      await connectDB();
      await SubagentTask.updateOne(
        { task_id },
        { $push: { debug_log: { $each: batch, $slice: -200 } } }
      );
    } catch (err) {
      pending.unshift(...batch);
      console.error(`[SubagentRunner:${task_id}] log flush failed:`, err);
    }
  }

  let lastProgressWrite = 0;
  let step = 0;
  let lastNote = 'Starting...';
  let fullResponse = '';
  let totalSseEvents = 0;

  try {
    const chatUrl = `${base_url}/api/chat`;
    log(`fetch start persona=${persona_id} max_turns=${max_turns}`);

    const res = await fetch(chatUrl, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${jwt_token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        messages: [{ role: 'user', content: message }],
        personaId: persona_id,
        is_subagent: true,
        execution_mode: execution_mode ?? 'autonomous',
        learning_preload_block,
        last_run_lessons_block,
        thread_id,
        max_turns,
      }),
      signal: AbortSignal.timeout(9 * 60 * 1000),
    });

    log(`http status=${res.status} ok=${res.ok}`);
    await flushLog(true);

    if (!res.ok || !res.body) {
      throw new Error(`Chat API returned ${res.status}: ${res.statusText}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let currentEventType = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEventType = line.slice(7).trim();
          continue;
        }

        if (line.startsWith('data: ')) {
          const data = line.slice(6);
          if (data === '[DONE]') { currentEventType = ''; continue; }

          totalSseEvents++;

          try {
            const parsed = JSON.parse(data);
            if (currentEventType === 'text' && parsed.delta) {
              fullResponse += parsed.delta;
            } else if (currentEventType === 'tool-call-start') {
              step++;
              lastNote = `Using tool: ${parsed.name || 'working'}...`;
              log(`sse tool-call-start name=${parsed.name} step=${step}`);
            } else if (currentEventType === 'tool-call-end') {
              log(`sse tool-call-end name=${parsed.name}`);
            } else if (currentEventType === 'usage') {
              log(`sse usage input=${parsed.input_tokens} output=${parsed.output_tokens}`);
              await connectDB();
              await SubagentTask.updateOne(
                { task_id },
                { $set: { tokens: { input: parsed.input_tokens, output: parsed.output_tokens, total: (parsed.input_tokens || 0) + (parsed.output_tokens || 0) } } }
              );
            } else if (currentEventType === 'error') {
              log(`sse error: ${JSON.stringify(parsed).slice(0, 300)}`);
            }
          } catch {
            // non-JSON data lines — skip
          }

          currentEventType = '';
        }

        const now = Date.now();
        const needsLogFlush = now - lastLogFlush > LOG_FLUSH_MS && pending.length > 0;
        const needsProgressWrite = firestoreConfigured && now - lastProgressWrite > PROGRESS_THROTTLE_MS;

        if (needsProgressWrite) {
          lastProgressWrite = now;
          try {
            const db = getAdminFirestore();
            await db.doc(`subagent_tasks/${user_id}/active/${task_id}`).set({
              task_id, thread_id, status: 'running',
              progress: { step, note: lastNote, updated_at: new Date().toISOString() },
              started_at: new Date().toISOString(),
            }, { merge: true });
          } catch (err) {
            log(`firestore progress FAILED: ${(err as Error).message}`);
          }
        }

        if (needsLogFlush) await flushLog(true);
      }
    }

    log(`stream done total_events=${totalSseEvents} response_len=${fullResponse.length}`);
    await flushLog(true);
    await connectDB();

    const existing = await SubagentTask.findOne({ task_id }, { assessment: 1 }).lean();
    const hasAssessment = (existing as any)?.assessment?.status != null;

    let finalStatus: 'done' | 'failed';
    if (hasAssessment) {
      finalStatus = (existing as any).assessment.status === 'failed' ? 'failed' : 'done';
    } else {
      finalStatus = fullResponse.trimStart().startsWith('ERROR:') ? 'failed' : 'done';
    }

    await SubagentTask.updateOne(
      { task_id },
      {
        $set: {
          status: finalStatus,
          message: fullResponse || 'Task completed.',
          result: finalStatus === 'done' ? fullResponse : undefined,
          error: finalStatus === 'failed' ? fullResponse : undefined,
          completed_at: new Date(),
          progress: { step, note: finalStatus === 'failed' ? 'Failed' : 'Done', updated_at: new Date() },
        },
      }
    );

    await ConversationThread.findOneAndUpdate(
      { thread_id, user_id },
      {
        $set: { status: 'completed', updated_at: new Date(), last_accessed_at: new Date() },
        $setOnInsert: {
          messages: [
            { role: 'user', content: message },
            { role: 'assistant', content: fullResponse },
          ],
        },
      },
      { upsert: true }
    );

    if (firestoreConfigured) {
      try {
        const db = getAdminFirestore();
        const firestoreData: Record<string, unknown> = {
          status: finalStatus,
          completed_at: new Date().toISOString(),
          progress: { step, note: finalStatus === 'failed' ? 'Failed' : 'Done', updated_at: new Date().toISOString() },
        };
        if (finalStatus === 'failed') firestoreData.error = fullResponse;
        else firestoreData.result_summary = fullResponse;
        await db.doc(`subagent_tasks/${user_id}/active/${task_id}`).set(firestoreData, { merge: true });
      } catch (err) {
        log(`firestore completion FAILED: ${(err as Error).message}`);
      }

      const responseSummary = fullResponse.length > 2000 ? fullResponse.slice(0, 2000) + '\n…(truncated)' : fullResponse;
      const notifSummary = finalStatus === 'failed'
        ? `Subagent "${label || 'task'}" failed.\n\nFinal response:\n${responseSummary}`
        : `Subagent "${label || 'task'}" is done.\n\nFinal response:\n${responseSummary}`;

      await pushNotification({
        user_id,
        type: finalStatus === 'failed' ? 'subagent.failed' : 'subagent.completed',
        source: `subagent:${task_id}`,
        priority: 'normal',
        summary: notifSummary,
        payload_ref: task_id,
      }).catch(err => log(`pushNotification FAILED: ${err?.message}`));
    }

    await flushLog(true);

  } catch (err: any) {
    log(`FATAL error: ${err?.message || 'Unknown error'}`);
    console.error(`[SubagentRunner] task ${task_id} failed:`, err);

    await flushLog(true);
    await connectDB();

    const existing = await SubagentTask.findOne({ task_id }, { assessment: 1 }).lean();
    const hasAssessment = (existing as any)?.assessment != null;
    const isCleanTermination = hasAssessment || err?.message === 'terminated';

    if (isCleanTermination) {
      const assessmentStatus = (existing as any)?.assessment?.status;
      const finalStatus = assessmentStatus === 'failed' ? 'failed' : 'done';
      await SubagentTask.updateOne(
        { task_id },
        {
          $set: {
            status: finalStatus,
            message: fullResponse || 'Task completed.',
            result: finalStatus === 'done' ? fullResponse : undefined,
            error: finalStatus === 'failed' ? fullResponse : undefined,
            completed_at: new Date(),
            progress: { step, note: finalStatus === 'failed' ? 'Failed' : 'Done', updated_at: new Date() },
          },
        }
      );
      return;
    }

    const errorMsg = err?.message || 'Unknown error';

    await SubagentTask.updateOne(
      { task_id },
      { $set: { status: 'failed', message: `Task failed: ${errorMsg}`, error: errorMsg, completed_at: new Date() } }
    );

    if (firestoreConfigured) {
      try {
        const db = getAdminFirestore();
        await db.doc(`subagent_tasks/${user_id}/active/${task_id}`).set(
          { status: 'failed', error: errorMsg, completed_at: new Date().toISOString() },
          { merge: true }
        );
      } catch { /* non-fatal */ }

      await pushNotification({
        user_id,
        type: 'subagent.failed',
        source: `subagent:${task_id}`,
        priority: 'high',
        summary: `Subagent task failed: ${errorMsg}`,
        payload_ref: task_id,
      }).catch(() => { /* non-fatal */ });
    }
  }
}
