"use client";

import { useCallback, useRef, useState } from "react";
import type { ChatSource, ChatStreamEvent } from "@/lib/api-types";

// One message in the visible thread. Backend events feed into this:
//   - `token` events append to `content`
//   - `final` event sets `sources` + finalizes `content`
//   - `hitl_interrupt` sets the interrupt fields (visible answer stops streaming)
//   - `error` sets error
// The `nodeStatus` is the current LangGraph node label ("Searching documents…")
// so the UI can show node-progress without inlining it into the message text.
export interface ChatTurn {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  confidence?: number | null;
  status?: "streaming" | "done" | "error" | "interrupted";
  nodeLabel?: string;
  error?: string;
  interrupt?: { reason: string; answer_preview: string; thread_id: string };
}

function randomId() {
  return `msg_${Math.random().toString(36).slice(2, 10)}`;
}

// Parse a single `data:` payload into the structured event. Returns null on
// malformed lines so callers can skip them.
function parseSSE(raw: string): ChatStreamEvent | null {
  try {
    return JSON.parse(raw) as ChatStreamEvent;
  } catch {
    return null;
  }
}

export interface UseStreamChat {
  messages: ChatTurn[];
  threadId: string | null;
  isStreaming: boolean;
  send: (message: string) => Promise<void>;
  abort: () => void;
  reset: () => void;
  hydrate: (messages: ChatTurn[], threadId: string | null) => void;
}

export function useStreamChat(initialThreadId: string | null = null): UseStreamChat {
  const [messages, setMessages] = useState<ChatTurn[]>([]);
  const [threadId, setThreadId] = useState<string | null>(initialThreadId);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const updateAssistant = useCallback((id: string, patch: Partial<ChatTurn> | ((t: ChatTurn) => ChatTurn)) => {
    setMessages((prev) =>
      prev.map((m) => {
        if (m.id !== id) return m;
        return typeof patch === "function" ? patch(m) : { ...m, ...patch };
      }),
    );
  }, []);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || isStreaming) return;

      const userTurn: ChatTurn = { id: randomId(), role: "user", content: trimmed };
      const assistantId = randomId();
      const assistantTurn: ChatTurn = {
        id: assistantId,
        role: "assistant",
        content: "",
        status: "streaming",
      };
      setMessages((prev) => [...prev, userTurn, assistantTurn]);
      setIsStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const res = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
          body: JSON.stringify({ message: trimmed, thread_id: threadId ?? undefined }),
          signal: controller.signal,
        });

        if (!res.ok || !res.body) {
          const body = await res.text().catch(() => "");
          updateAssistant(assistantId, { status: "error", error: body || `HTTP ${res.status}` });
          setIsStreaming(false);
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        // SSE frame parsing: events are separated by blank lines.
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let idx;
          while ((idx = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);

            // A frame can have multiple lines; we only care about `data:` lines.
            for (const line of frame.split("\n")) {
              if (!line.startsWith("data:")) continue;
              const raw = line.slice(5).trim();
              if (!raw) continue;
              const evt = parseSSE(raw);
              if (!evt) continue;

              if (evt.type === "node_start") {
                updateAssistant(assistantId, { nodeLabel: evt.label });
              } else if (evt.type === "node_end") {
                // No-op; node_start of the next node will replace the label.
              } else if (evt.type === "token") {
                updateAssistant(assistantId, (t) => ({ ...t, content: t.content + evt.content }));
              } else if (evt.type === "hitl_interrupt") {
                setThreadId(evt.thread_id);
                updateAssistant(assistantId, {
                  status: "interrupted",
                  nodeLabel: undefined,
                  interrupt: {
                    reason: evt.reason,
                    answer_preview: evt.answer_preview,
                    thread_id: evt.thread_id,
                  },
                });
              } else if (evt.type === "final") {
                setThreadId(evt.thread_id);
                updateAssistant(assistantId, {
                  status: "done",
                  nodeLabel: undefined,
                  // `response` is authoritative — it wins over accumulated tokens
                  // because the generator may have emitted formatting fixes only
                  // present in the final response.
                  content: evt.response || (assistantTurn.content as string),
                  sources: evt.sources,
                  confidence: evt.confidence,
                });
              } else if (evt.type === "error") {
                updateAssistant(assistantId, { status: "error", error: evt.message, nodeLabel: undefined });
              }
            }
          }
        }

        // Finalize anything still marked "streaming" — backends sometimes close
        // a stream without a `final` event (e.g. graceful timeout).
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId && m.status === "streaming" ? { ...m, status: "done", nodeLabel: undefined } : m)),
        );
      } catch (e) {
        if ((e as Error)?.name === "AbortError") {
          updateAssistant(assistantId, { status: "done", nodeLabel: undefined });
        } else {
          updateAssistant(assistantId, {
            status: "error",
            error: e instanceof Error ? e.message : "Stream failed",
            nodeLabel: undefined,
          });
        }
      } finally {
        abortRef.current = null;
        setIsStreaming(false);
      }
    },
    [isStreaming, threadId, updateAssistant],
  );

  const abort = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setThreadId(null);
  }, []);

  const hydrate = useCallback((msgs: ChatTurn[], tid: string | null) => {
    abortRef.current?.abort();
    setMessages(msgs);
    setThreadId(tid);
  }, []);

  return { messages, threadId, isStreaming, send, abort, reset, hydrate };
}
