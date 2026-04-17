import { getAuthFromRequest } from './jwt';

interface AuthResult {
  sub: string;
  email?: string;
  name?: string;
  [key: string]: any;
}

/**
 * Dual-auth for internal API routes called by the MCP bridge.
 *
 * 1. If X-Admin-Key matches MCP_ADMIN_KEY → trusted service-to-service call.
 *    User identity is decoded (not verified) from the Bearer JWT.
 * 2. Otherwise, fall back to full JWT verification (direct API calls, curl).
 */
export function authenticateInternalRequest(req: Request): AuthResult | null {
  const adminKey = req.headers.get('x-admin-key');
  const expectedKey = process.env.MCP_ADMIN_KEY;

  if (adminKey && expectedKey && adminKey === expectedKey) {
    const authHeader = req.headers.get('authorization');
    if (!authHeader?.startsWith('Bearer ')) return null;

    try {
      const parts = authHeader.substring(7).split('.');
      if (parts.length !== 3) return null;
      return JSON.parse(Buffer.from(parts[1], 'base64').toString());
    } catch {
      return null;
    }
  }

  return getAuthFromRequest(req);
}
