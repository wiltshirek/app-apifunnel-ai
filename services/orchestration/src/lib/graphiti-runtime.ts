/**
 * Graphiti / Learning graph service URL resolution.
 *
 * In the original Next.js app this was limited to `npm run dev` only.
 * On this standalone server we use an env var so it works in all environments.
 */
export function getGraphitiUrl(): string | null {
  return process.env.GRAPHITI_SERVICE_URL || null;
}

export function graphitiDisabledReason(): string {
  return 'GRAPHITI_SERVICE_URL is not set';
}
