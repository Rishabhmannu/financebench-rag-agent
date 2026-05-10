"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { LogOut, User } from "lucide-react";
import { toast } from "sonner";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ThemeToggle } from "@/components/theme-toggle";
import type { UserMe } from "@/lib/api-types";

function initials(name: string) {
  return name
    .split(/\s+/)
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

function roleVariant(role: string): "default" | "secondary" | "destructive" | "outline" {
  if (role === "admin") return "destructive";
  if (role === "c_level") return "default";
  return "secondary";
}

export function AppHeader() {
  const router = useRouter();
  const [me, setMe] = useState<UserMe | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/auth/me")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!cancelled) setMe(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    toast.success("Signed out");
    router.replace("/login");
    router.refresh();
  }

  return (
    <header className="flex h-14 items-center justify-between gap-4 border-b bg-background px-4 md:px-6">
      <Link href="/chat" className="font-semibold tracking-tight">
        RAG Agent
      </Link>

      <div className="flex items-center gap-2">
        {me?.role === "admin" && (
          <Link
            href="/admin"
            className="inline-flex h-7 items-center rounded-md px-2.5 text-[0.8rem] font-medium hover:bg-muted hover:text-foreground"
          >
            Admin
          </Link>
        )}
        <ThemeToggle />
        <DropdownMenu>
          <DropdownMenuTrigger
            className="inline-flex h-7 items-center gap-2 rounded-md px-2 text-[0.8rem] font-medium hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          >
            <Avatar className="h-6 w-6">
              <AvatarFallback className="text-[10px]">
                {me ? initials(me.name) : <User className="h-3 w-3" />}
              </AvatarFallback>
            </Avatar>
            <span className="hidden sm:inline-flex items-center gap-2">
              {me?.name ?? "Loading…"}
              {me && <Badge variant={roleVariant(me.role)}>{me.role}</Badge>}
            </span>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel>
              <div className="flex flex-col">
                <span>{me?.name ?? ""}</span>
                <span className="text-xs font-normal text-muted-foreground">{me?.department ?? ""}</span>
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={logout} className="text-destructive focus:text-destructive">
              <LogOut className="mr-2 h-4 w-4" />
              Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
