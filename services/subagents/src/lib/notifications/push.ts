import { getAdminFirestore, firestoreConfigured } from '../firebase-admin';
import type { AgentNotification, CreateNotificationInput } from './types';

export async function pushNotification(input: CreateNotificationInput): Promise<string> {
  const id = `notif_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`;

  if (!firestoreConfigured) return id;

  const expiresInMs = (input.expires_in_hours ?? 24) * 60 * 60 * 1000;
  const now = new Date();

  const notification: AgentNotification = {
    id,
    user_id:     input.user_id,
    type:        input.type,
    source:      input.source,
    priority:    input.priority ?? 'normal',
    summary:     input.summary,
    payload_ref: input.payload_ref,
    payload_url: input.payload_url,
    status:      'pending',
    created_at:  now.toISOString(),
    delivered_at: null,
    expires_at:  new Date(now.getTime() + expiresInMs).toISOString(),
  };

  try {
    const db = getAdminFirestore();
    await db.doc(`notifications/${input.user_id}/pending/${id}`).set(notification);
  } catch (err) {
    console.error('[pushNotification] Firestore write failed (non-fatal):', err);
  }

  return id;
}
