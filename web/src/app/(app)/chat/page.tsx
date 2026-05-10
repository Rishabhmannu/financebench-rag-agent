"use client";

import { useEffect, useRef } from "react";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ChatInput } from "@/components/chat/chat-input";
import { ChatMessage } from "@/components/chat/chat-message";
import { useStreamChat } from "@/hooks/use-stream-chat";

const EXAMPLES = [
  "What was Apple's total revenue in fiscal year 2023?",
  "What is the maximum daily travel expense allowed?",
  "Summarize Microsoft's FY2023 operating income drivers",
];

export default function ChatPage() {
  const { messages, isStreaming, send, abort, reset } = useStreamChat();
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  const empty = messages.length === 0;

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center justify-between border-b px-4 py-2 md:px-6">
        <h1 className="text-sm font-medium text-muted-foreground">
          {empty ? "New conversation" : "Conversation"}
        </h1>
        <Button variant="ghost" size="sm" onClick={reset} disabled={empty && !isStreaming}>
          <Plus className="mr-1.5 h-4 w-4" />
          New
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-4 py-6 md:px-6">
          {empty ? (
            <EmptyState onPick={(t) => send(t)} disabled={isStreaming} />
          ) : (
            <div className="space-y-6">
              {messages.map((m) => (
                <ChatMessage key={m.id} turn={m} />
              ))}
              <div ref={endRef} />
            </div>
          )}
        </div>
      </div>

      <div className="border-t bg-background">
        <div className="mx-auto max-w-3xl px-4 py-3 md:px-6">
          <ChatInput onSend={send} onAbort={abort} isStreaming={isStreaming} />
          <p className="mt-2 text-center text-[11px] text-muted-foreground">
            Answers are grounded in retrieved documents with hallucination checking. Verify high-stakes figures against the source PDF.
          </p>
        </div>
      </div>
    </div>
  );
}

function EmptyState({ onPick, disabled }: { onPick: (text: string) => void; disabled: boolean }) {
  return (
    <div className="flex flex-col items-center gap-6 py-16 text-center">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Ask the RAG agent</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          RBAC-scoped Q&amp;A over your financial documents. Try one of these:
        </p>
      </div>
      <div className="grid w-full max-w-2xl gap-2 sm:grid-cols-2">
        {EXAMPLES.map((e) => (
          <button
            key={e}
            disabled={disabled}
            onClick={() => onPick(e)}
            className="rounded-lg border bg-background p-3 text-left text-sm transition hover:bg-accent hover:text-accent-foreground disabled:cursor-not-allowed disabled:opacity-60"
          >
            {e}
          </button>
        ))}
      </div>
    </div>
  );
}
