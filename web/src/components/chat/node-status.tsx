"use client";

import { Loader2 } from "lucide-react";

export function NodeStatus({ label }: { label?: string }) {
  if (!label) return null;
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      <span>{label}…</span>
    </div>
  );
}
