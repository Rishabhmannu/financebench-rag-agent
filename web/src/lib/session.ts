// Server-only helpers for the JWT session cookie. The cookie is httpOnly +
// SameSite=Lax, so client JS never sees the token — Route Handlers and
// proxy.ts read it via next/headers cookies().
//
// We do NOT verify the JWT signature here (the backend is the source of
// truth and re-verifies on every request). We only decode it to display
// user info in the header without an extra /auth/me roundtrip when we
// already have it on the request.

import { cookies } from "next/headers";
import { env } from "./env";
import type { Role } from "./api-types";

export interface SessionPayload {
  sub: string; // user_id
  name: string;
  role: Role;
  department: string;
  exp: number; // unix seconds
}

const COOKIE_OPTIONS = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure: process.env.NODE_ENV === "production",
  path: "/",
};

export async function setSessionCookie(token: string): Promise<void> {
  const jar = await cookies();
  jar.set(env.sessionCookieName, token, {
    ...COOKIE_OPTIONS,
    maxAge: env.sessionTtlSeconds,
  });
}

export async function clearSessionCookie(): Promise<void> {
  const jar = await cookies();
  jar.set(env.sessionCookieName, "", { ...COOKIE_OPTIONS, maxAge: 0 });
}

export async function getSessionToken(): Promise<string | null> {
  const jar = await cookies();
  return jar.get(env.sessionCookieName)?.value ?? null;
}

export function decodeJwtUnsafe(token: string): SessionPayload | null {
  // Base64url-decode the JWT payload. We deliberately do not verify the
  // signature — that's the backend's job. This is only for client-side UX
  // (showing the user's name in the header). Any decision based on this
  // must be re-confirmed by the backend.
  try {
    const [, payload] = token.split(".");
    if (!payload) return null;
    const padded = payload.replace(/-/g, "+").replace(/_/g, "/");
    const json = Buffer.from(padded, "base64").toString("utf8");
    return JSON.parse(json) as SessionPayload;
  } catch {
    return null;
  }
}

export async function getSession(): Promise<SessionPayload | null> {
  const tok = await getSessionToken();
  if (!tok) return null;
  const decoded = decodeJwtUnsafe(tok);
  if (!decoded) return null;
  if (decoded.exp * 1000 < Date.now()) return null;
  return decoded;
}
