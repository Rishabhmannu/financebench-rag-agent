// Backend contract types. Hand-mirrored from src/models/schemas.py and the
// route handlers in src/api/routes/. Keep in sync — or regenerate via
// `npx openapi-typescript http://localhost:8000/openapi.json -o src/lib/api-types.gen.ts`
// once the backend is running and replace this file.

export type Role = "analyst" | "finance" | "hr" | "c_level" | "admin";

export interface UserPermissions {
  allowed_doc_types: string[];
  allowed_confidentiality: string[];
  max_results: number;
  requires_hitl_above: number | null;
}

export interface UserMe {
  user_id: string;
  name: string;
  role: Role;
  department: string;
  permissions: UserPermissions;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  user_id: string;
  name: string;
  role: Role;
  department: string;
}

export interface ChatSource {
  file: string;
  page?: number | string | null;
  [k: string]: unknown;
}

export interface ChatRequest {
  message: string;
  thread_id?: string;
}

// SSE event shapes from POST /chat/stream (see src/api/routes/chat.py)
export type ChatStreamEvent =
  | { type: "node_start"; node: string; label: string }
  | { type: "node_end"; node: string }
  | { type: "token"; content: string }
  | {
      type: "hitl_interrupt";
      thread_id: string;
      reason: string;
      answer_preview: string;
    }
  | {
      type: "final";
      response: string;
      sources: ChatSource[];
      confidence: number | null;
      requires_approval: boolean;
      thread_id: string;
    }
  | { type: "error"; message: string };

export interface ThreadSummary {
  thread_id: string;
  title: string;
  checkpoint_count: number;
  is_interrupted: boolean;
}

export interface ThreadList {
  threads: ThreadSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ThreadMessage {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  confidence?: number | null;
}

export interface ThreadDetail {
  thread_id: string;
  messages: ThreadMessage[];
  is_interrupted: boolean;
  interrupt_payload: { reason?: string; answer_preview?: string } | null;
}

export interface CostBucket {
  key: string | null;
  calls: number;
  cost_usd: number;
  tokens: number;
}

export interface AdminCosts {
  window_days: number;
  start: string;
  end: string;
  total: { calls: number; cost_usd: number; tokens: number };
  by_user: CostBucket[];
  by_model: CostBucket[];
  by_trace_name: CostBucket[];
}

export interface AdminUser {
  username: string;
  name: string;
  role: Role;
  department: string;
}

export interface RoleConfig {
  name: string;
  allowed_doc_types: string[];
  allowed_confidentiality: string[];
  max_results: number;
  requires_hitl_above: number | null;
  is_system: boolean;
}
