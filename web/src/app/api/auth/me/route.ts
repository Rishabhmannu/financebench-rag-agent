import { NextResponse } from "next/server";
import { ApiError, backendJson } from "@/lib/api";
import type { UserMe } from "@/lib/api-types";

export async function GET() {
  try {
    const me = await backendJson<UserMe>("/auth/me");
    return NextResponse.json(me);
  } catch (e) {
    if (e instanceof ApiError) {
      return NextResponse.json({ detail: e.detail }, { status: e.status });
    }
    return NextResponse.json({ detail: "Upstream error" }, { status: 502 });
  }
}
