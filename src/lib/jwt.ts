import jwt from 'jsonwebtoken';

const JWT_SECRET = process.env.JWT_SECRET || 'unsigned-trust-relationship';

export type UserRole = 'system_admin' | 'enterprise_admin' | 'user';

export type ChannelType = 'web' | 'extension' | 'gptapps' | 'cursor' | 'sms' | 'whatsapp' | 'voice' | 'iot' | 'heartbeat';

export type ApiSettingsMap = Record<string, { resource_url?: string; [key: string]: any }>;

export interface AuthPayload {
  sub: string;
  iss?: string;
  aud?: string;
  exp: number;
  iat: number;
  email: string;
  name?: string;
  instance_id: string;
  tier: 'demo' | 'freemium' | 'basic' | 'premium' | 'enterprise';
  role: UserRole;
  payment_status?: 'free' | 'paid';
  client_type?: ChannelType;
  api_settings?: ApiSettingsMap;
  enabled_servers?: string[];
  scheduled_task_id?: string;
  subagent_task_id?: string;
  user_id: string;
}

export function verifyToken(token: string): AuthPayload | null {
  try {
    const decoded = jwt.decode(token) as AuthPayload;
    if (!decoded || !decoded.sub || !decoded.iat || !decoded.exp) return null;
    if (!decoded.role) decoded.role = 'user';
    return decoded;
  } catch {
    return null;
  }
}

export function getAuthFromRequest(req: Request): AuthPayload | null {
  const authHeader = req.headers.get('authorization');
  if (!authHeader?.startsWith('Bearer ')) return null;
  return verifyToken(authHeader.substring(7));
}

export function mintAppJwt(
  auth: Partial<AuthPayload>,
  enabledServers: string[],
  apiSettings: ApiSettingsMap,
): string {
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    sub: auth.sub || auth.user_id || 'anonymous',
    iat: now,
    exp: now + 3600,
    iss: 'https://mcp-platform.local',
    aud: 'mcp-research-server',
    email: auth.email || 'anonymous@apifunnel.ai',
    instance_id: auth.instance_id || 'public',
    tier: auth.tier,
    role: auth.role,
    payment_status: auth.payment_status,
    client_type: auth.client_type || 'web',
    enabled_servers: enabledServers,
    api_settings: apiSettings,
    user_id: auth.sub || auth.user_id || 'anonymous',
    ...(auth.scheduled_task_id && { scheduled_task_id: auth.scheduled_task_id }),
    ...(auth.subagent_task_id && { subagent_task_id: auth.subagent_task_id }),
  };
  return jwt.sign(payload, JWT_SECRET);
}
