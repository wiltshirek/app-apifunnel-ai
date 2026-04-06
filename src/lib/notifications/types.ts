export interface AgentNotification {
  id: string;
  user_id: string;
  type: NotificationType;
  source: string;
  priority: 'low' | 'normal' | 'high' | 'urgent';
  summary: string;
  payload_ref?: string;
  payload_url?: string;
  status: 'pending' | 'delivered' | 'expired';
  created_at: string;
  delivered_at?: string | null;
  expires_at?: string | null;
}

export type NotificationType =
  | 'subagent.completed'
  | 'subagent.failed'
  | 'subagent.running'
  | 'slack.message'
  | 'webhook.received'
  | 'schedule.completed'
  | 'email.received'
  | 'oauth.expired'
  | 'system.alert'
  | string;

export type CreateNotificationInput = Omit<AgentNotification,
  'id' | 'status' | 'created_at' | 'delivered_at'
> & {
  expires_in_hours?: number;
};
