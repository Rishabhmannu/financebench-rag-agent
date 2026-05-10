"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";

const schema = z.object({
  username: z.string().min(1, "Username is required"),
  password: z.string().min(1, "Password is required"),
});
type FormValues = z.infer<typeof schema>;

const DEV_USERS = [
  { username: "analyst", role: "Public 10-K only" },
  { username: "finance", role: "10-K + invoices + policies" },
  { username: "hr", role: "Expense policies" },
  { username: "clevel", role: "All incl. confidential" },
  { username: "admin", role: "Full access + admin panel" },
];

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="flex flex-1 items-center justify-center">Loading…</div>}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = searchParams.get("next") || "/chat";
  const [submitting, setSubmitting] = useState(false);

  const {
    register,
    handleSubmit,
    setValue,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { username: "", password: "" },
  });

  async function onSubmit(values: FormValues) {
    setSubmitting(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        toast.error(body?.detail ?? "Login failed");
        setSubmitting(false);
        return;
      }
      toast.success("Signed in");
      router.replace(next);
      router.refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Login failed");
      setSubmitting(false);
    }
  }

  function fillUser(username: string) {
    setValue("username", username);
    setValue("password", `${username}123`);
  }

  return (
    <div className="flex flex-1 items-center justify-center px-4 py-12">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-2xl">RAG Agent</CardTitle>
          <CardDescription>Enterprise financial document Q&A</CardDescription>
        </CardHeader>
        <form onSubmit={handleSubmit(onSubmit)} noValidate>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">Username</Label>
              <Input id="username" autoComplete="username" autoFocus {...register("username")} />
              {errors.username && <p className="text-sm text-destructive">{errors.username.message}</p>}
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input id="password" type="password" autoComplete="current-password" {...register("password")} />
              {errors.password && <p className="text-sm text-destructive">{errors.password.message}</p>}
            </div>

            <details className="rounded-md border bg-muted/30 p-3 text-sm">
              <summary className="cursor-pointer select-none font-medium">Dev test accounts</summary>
              <ul className="mt-3 space-y-1">
                {DEV_USERS.map((u) => (
                  <li key={u.username} className="flex items-center justify-between gap-3">
                    <span>
                      <code className="font-mono">{u.username}</code>{" "}
                      <span className="text-muted-foreground">— {u.role}</span>
                    </span>
                    <Button type="button" variant="ghost" size="sm" onClick={() => fillUser(u.username)}>
                      Use
                    </Button>
                  </li>
                ))}
              </ul>
              <p className="mt-3 text-xs text-muted-foreground">
                Passwords are <code>&lt;username&gt;123</code>.
              </p>
            </details>
          </CardContent>
          <CardFooter>
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
          </CardFooter>
        </form>
      </Card>
    </div>
  );
}
