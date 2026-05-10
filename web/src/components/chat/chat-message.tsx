"use client";

import { AlertCircle, User2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { NodeStatus } from "./node-status";
import { SourceChips } from "./source-chips";
import type { ChatTurn } from "@/hooks/use-stream-chat";

function AssistantAvatar() {
  return (
    <Avatar className="h-7 w-7 shrink-0">
      <AvatarFallback className="bg-primary text-primary-foreground text-[11px] font-semibold">RA</AvatarFallback>
    </Avatar>
  );
}

function UserAvatar() {
  return (
    <Avatar className="h-7 w-7 shrink-0">
      <AvatarFallback>
        <User2 className="h-3.5 w-3.5" />
      </AvatarFallback>
    </Avatar>
  );
}

export function ChatMessage({ turn }: { turn: ChatTurn }) {
  const isUser = turn.role === "user";

  return (
    <div className={cn("flex gap-3 px-2", isUser && "flex-row-reverse")}>
      {isUser ? <UserAvatar /> : <AssistantAvatar />}
      <div className={cn("flex-1 max-w-[calc(100%-3rem)] space-y-2", isUser && "flex flex-col items-end")}>
        <div
          className={cn(
            "rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap break-words",
            isUser ? "bg-primary text-primary-foreground" : "bg-muted",
          )}
        >
          {turn.content || (!isUser && turn.status === "streaming" && !turn.nodeLabel ? "…" : null)}
        </div>

        {!isUser && turn.status === "streaming" && <NodeStatus label={turn.nodeLabel} />}

        {!isUser && turn.status === "done" && <SourceChips sources={turn.sources} />}

        {!isUser && turn.status === "error" && (
          <div className="flex items-center gap-2 text-sm text-destructive">
            <AlertCircle className="h-3.5 w-3.5" />
            <span>{turn.error ?? "Something went wrong."}</span>
          </div>
        )}
      </div>
    </div>
  );
}
