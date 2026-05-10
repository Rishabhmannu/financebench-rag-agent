// Centralised env access so we get a friendly error if something's missing
// instead of surfacing `undefined` deep inside a request handler.

function required(name: string, fallback?: string): string {
  const v = process.env[name] ?? fallback;
  if (!v) {
    throw new Error(`Missing required env var: ${name}`);
  }
  return v;
}

export const env = {
  backendUrl: required("BACKEND_URL", "http://localhost:8000").replace(/\/$/, ""),
  sessionCookieName: required("SESSION_COOKIE_NAME", "rag_session"),
  sessionTtlSeconds: Number(required("SESSION_TTL_SECONDS", "86400")),
};
