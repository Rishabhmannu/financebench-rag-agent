import { env } from "@/lib/env";
import { getSessionToken } from "@/lib/session";

// SSE pass-through. We don't parse or buffer events — the backend already
// emits well-formed `data: {...}\n\n` frames; we just attach the auth header
// and stream the response body straight to the browser.
//
// Two reasons we can't use the regular `backendFetch` helper:
//   1. We need to forward `response.body` as a ReadableStream, not await JSON.
//   2. SSE wants no caching and `text/event-stream` content type preserved.

export async function POST(request: Request) {
  const token = await getSessionToken();
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  const body = await request.text();

  let upstream: Response;
  try {
    upstream = await fetch(`${env.backendUrl}/chat/stream`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body,
      cache: "no-store",
    });
  } catch (e) {
    // Connection failure — emit a one-frame SSE error so the client surfaces it
    // in the chat thread instead of a silent network failure.
    const msg = e instanceof Error ? e.message : String(e);
    const payload = JSON.stringify({
      type: "error",
      message: `Backend unreachable at ${env.backendUrl}. Is the API server running? (${msg})`,
    });
    return new Response(`data: ${payload}\n\n`, {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
      },
    });
  }

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text().catch(() => "");
    return new Response(text || "Upstream error", { status: upstream.status });
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no", // disable nginx buffering if anything sits in front
    },
  });
}
