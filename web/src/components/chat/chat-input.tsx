"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUp, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Props {
  onSend: (text: string) => void;
  onAbort?: () => void;
  isStreaming: boolean;
  disabled?: boolean;
  placeholder?: string;
}

export function ChatInput({ onSend, onAbort, isStreaming, disabled, placeholder }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
  }, [value]);

  function submit() {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || disabled) return;
    onSend(trimmed);
    setValue("");
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className={cn("flex items-end gap-2 rounded-2xl border bg-background p-2 shadow-sm", disabled && "opacity-60")}>
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder ?? "Ask about financial documents…"}
        disabled={disabled}
        rows={1}
        className="flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-hidden placeholder:text-muted-foreground"
      />
      {isStreaming && onAbort ? (
        <Button type="button" size="icon" variant="secondary" onClick={onAbort} aria-label="Stop">
          <Square className="h-4 w-4" />
        </Button>
      ) : (
        <Button type="button" size="icon" onClick={submit} disabled={!value.trim() || disabled} aria-label="Send">
          <ArrowUp className="h-4 w-4" />
        </Button>
      )}
    </div>
  );
}
