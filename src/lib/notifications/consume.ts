import { getAdminFirestore, firestoreConfigured } from '../firebase-admin';
import type { AgentNotification } from './types';

export async function consumePendingNotifications(userId: string): Promise<AgentNotification[]> {
  if (!firestoreConfigured) return [];

  try {
    const db = getAdminFirestore();
    const snapshot = await db
      .collection(`notifications/${userId}/pending`)
      .where('status', '==', 'pending')
      .orderBy('created_at', 'asc')
      .limit(10)
      .get();

    if (snapshot.empty) return [];

    const notifications: AgentNotification[] = [];
    const batch = db.batch();

    for (const doc of snapshot.docs) {
      notifications.push(doc.data() as AgentNotification);
      batch.delete(doc.ref);
    }

    await batch.commit();
    return notifications;
  } catch (err) {
    console.error('[consumePendingNotifications] failed:', err);
    return [];
  }
}
