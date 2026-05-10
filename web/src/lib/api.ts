// Server-side fetch wrapper used by BFF route handlers and Server
// Components. Attaches the user's JWT from the session cookie automatically.
//
// Client components must NEVER import from here — they call `/api/*` BFF
// endpoints on the Next.js side, never the backend directly. That way the
// JWT stays on the server.

import { env } from "./env";
import { getSessionToken } from "./session";

export interface BackendError {
  status: number;
  detail: string;
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`Backend ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

async function authHeaders(): Promise<HeadersInit> {
  const token = await getSessionToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function backendFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  const auth = await authHeaders();
  for (const [k, v] of Object.entries(auth)) headers.set(k, v as string);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`${env.backendUrl}${path}`, {
    ...init,
    headers,
    // BFF endpoints proxy live requests — no Next.js cache.
    cache: "no-store",
  });
}

export async function backendJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let res: Response;
  try {
    res = await backendFetch(path, init);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    throw new ApiError(
      502,
      `Backend unreachable at ${env.backendUrl}. Is the API server running? (${msg})`,
    );
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = (body?.detail as string) ?? detail;
    } catch {
      /* non-JSON body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}
