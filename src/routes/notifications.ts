import { Hono } from 'hono';
import { authenticateInternalRequest } from '../lib/auth-internal';
import { getAdminFirestore, firestoreConfigured } from '../lib/firebase-admin';
import { pushNotification } from '../lib/notifications/push';
import type { CreateNotificationInput } from '../lib/notifications/types';

export const notificationsRouter = new Hono();

// POST /v1/notifications — push a notification
notificationsRouter.post('/', async (c) => {
  if (!firestoreConfigured) return c.json({ error: 'Firebase not configured' }, 503);

  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const body = await c.req.json();
  if (!body.type || !body.summary || !body.source) {
    return c.json({ error: 'Missing required fields: type, summary, source' }, 400);
  }

  const input: CreateNotificationInput = {
    user_id:          auth.sub,
    type:             body.type,
    source:           body.source,
    priority:         body.priority ?? 'normal',
    summary:          body.summary,
    payload_ref:      body.payload_ref,
    payload_url:      body.payload_url,
    expires_in_hours: body.expires_in_hours ?? 24,
  };

  try {
    const id = await pushNotification(input);
    return c.json({ id });
  } catch (err) {
    console.error('[POST /v1/notifications] failed:', err);
    return c.json({ error: 'Failed to push notification' }, 500);
  }
});

// GET /v1/notifications — list pending notifications
notificationsRouter.get('/', async (c) => {
  if (!firestoreConfigured) return c.json({ notifications: [] });

  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  try {
    const db = getAdminFirestore();
    const snapshot = await db
      .collection(`notifications/${auth.sub}/pending`)
      .where('status', '==', 'pending')
      .orderBy('created_at', 'desc')
      .limit(50)
      .get();
    return c.json({ notifications: snapshot.docs.map(d => d.data()) });
  } catch (err) {
    console.error('[GET /v1/notifications] failed:', err);
    return c.json({ error: 'Failed to fetch notifications' }, 500);
  }
});

// POST /v1/notifications/:id/ack — acknowledge a notification
notificationsRouter.post('/:id/ack', async (c) => {
  if (!firestoreConfigured) return c.json({ error: 'Firebase not configured' }, 503);

  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);

  const id = c.req.param('id');

  try {
    const db = getAdminFirestore();
    const ref = db.doc(`notifications/${auth.sub}/pending/${id}`);
    const doc = await ref.get();
    if (!doc.exists) return c.json({ error: 'Not found' }, 404);
    await ref.delete();
    return c.json({ acknowledged: true });
  } catch (err) {
    console.error('[POST /v1/notifications/:id/ack] failed:', err);
    return c.json({ error: 'Failed to acknowledge notification' }, 500);
  }
});
