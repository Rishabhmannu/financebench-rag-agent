import { NextResponse } from "next/server";
import { env } from "@/lib/env";
import { setSessionCookie } from "@/lib/session";
import type { LoginRequest, TokenResponse } from "@/lib/api-types";

export async function POST(request: Request) {
  let body: LoginRequest;
  try {
    body = (await request.json()) as LoginRequest;
  } catch {
    return NextResponse.json({ detail: "Invalid JSON body" }, { status: 400 });
  }

  let backendRes: Response;
  try {
    backendRes = await fetch(`${env.backendUrl}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch (e) {
    // ECONNREFUSED / DNS failure / network error — the backend isn't reachable.
    // Surface this clearly so the user knows the credentials are not the problem.
    const msg = e instanceof Error ? e.message : String(e);
    return NextResponse.json(
      { detail: `Backend unreachable at ${env.backendUrl}. Is the API server running? (${msg})` },
      { status: 502 },
    );
  }

  if (!backendRes.ok) {
    let detail = "Login failed";
    try {
      const err = await backendRes.json();
      detail = (err?.detail as string) ?? detail;
    } catch {
      /* */
    }
    return NextResponse.json({ detail }, { status: backendRes.status });
  }

  const token = (await backendRes.json()) as TokenResponse;
  await setSessionCookie(token.access_token);

  // Don't echo the access_token back to the client — the cookie is enough.
  return NextResponse.json({
    user_id: token.user_id,
    name: token.name,
    role: token.role,
    department: token.department,
  });
}
