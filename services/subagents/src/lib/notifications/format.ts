import type { AgentNotification } from './types';

export function formatNotificationsAsSystemMessage(notifications: AgentNotification[]): string {
  if (notifications.length === 0) return '';

  const blocks = notifications.map(n => {
    const taskRef = n.payload_ref ? ` (task_id: ${n.payload_ref})` : '';
    return `[${n.type}]${taskRef}\n${n.summary}`;
  });

  return (
    `--- Agent Notifications (${notifications.length}) ---\n` +
    blocks.join('\n\n') + '\n\n' +
    `If you need more detail later, use get_subagent_status with the task_id. ` +
    `Any deliverables the subagent produced are in the lakehouse — use list_assets to browse them.`
  );
}
