"use client";

import { FileText } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { ChatSource } from "@/lib/api-types";

export function SourceChips({ sources }: { sources?: ChatSource[] }) {
  if (!sources || sources.length === 0) return null;

  return (
    <div className="mt-3 flex flex-wrap gap-1.5">
      <span className="text-xs font-medium text-muted-foreground self-center mr-1">Sources:</span>
      {sources.map((s, i) => {
        const page = s.page != null && s.page !== "" ? String(s.page) : null;
        const label = page ? `${s.file} · p. ${page}` : s.file;
        return (
          <a
            key={`${s.file}-${i}`}
            href={`/api/documents/${encodeURIComponent(s.file)}${page ? `#page=${page}` : ""}`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex"
          >
            <Badge variant="outline" className="gap-1 cursor-pointer hover:bg-accent">
              <FileText className="h-3 w-3" />
              <span className="font-mono text-[11px]">{label}</span>
            </Badge>
          </a>
        );
      })}
    </div>
  );
}
