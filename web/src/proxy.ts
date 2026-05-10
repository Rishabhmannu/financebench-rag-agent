// Next 16 renamed middleware.ts → proxy.ts (same functionality).
// We use it for one job: redirect unauthenticated users away from app
// pages and authenticated users away from the login page.
//
// Authorization itself stays on the backend — this only ensures the user
// has *some* session cookie before they reach a protected page. The
// backend re-verifies the JWT on every request.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const SESSION_COOKIE_NAME = process.env.SESSION_COOKIE_NAME ?? "rag_session";

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const hasCookie = Boolean(request.cookies.get(SESSION_COOKIE_NAME)?.value);

  // Already logged in but visiting /login → push to chat
  if (pathname === "/login" && hasCookie) {
    return NextResponse.redirect(new URL("/chat", request.url));
  }

  // Not logged in but visiting a protected page → push to /login
  if (!hasCookie && (pathname === "/" || pathname.startsWith("/chat") || pathname.startsWith("/admin"))) {
    const url = new URL("/login", request.url);
    if (pathname !== "/") url.searchParams.set("next", pathname);
    return NextResponse.redirect(url);
  }

  return NextResponse.next();
}

export const config = {
  // Skip Next internals and the API routes (those are auth-checked
  // server-side via the JWT cookie inside each handler).
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
