# RBAC Matrix

Role-based access control is enforced **at the vector DB level** via Qdrant payload filters — unauthorized documents are never retrieved, not just hidden after retrieval.

## Role → Permissions

Source: [src/config/rbac_config.py](../src/config/rbac_config.py)

| Role | Allowed Doc Types | Confidentiality Levels | Top-K | HITL Threshold |
|------|-------------------|------------------------|-------|----------------|
| `analyst` | `10k` | `public` | 5 | — (disabled) |
| `finance` | `10k`, `invoice`, `expense_policy` | `public`, `internal` | 10 | $100,000 |
| `hr` | `expense_policy` | `public`, `internal` | 5 | — (disabled) |
| `c_level` | `10k`, `invoice`, `expense_policy`, `board_report` | `public`, `internal`, `confidential` | 15 | $1,000,000 |
| `admin` | `*` (all) | `*` (all) | 20 | — (disabled) |

## What Each Doc Type Means

| Doc Type | Contents | Typical Confidentiality |
|----------|----------|-------------------------|
| `10k` | SEC 10-K annual filings (public company disclosures) | `public` |
| `invoice` | Company invoices from vendors | `internal` |
| `expense_policy` | Travel, procurement, reimbursement policies | `public` or `internal` |
| `board_report` | Board of directors materials, strategic memos | `confidential` |

## Confidentiality Levels

Detected automatically by [metadata_extractor.py](../src/ingestion/metadata_extractor.py) based on document text:

- **`public`** — default; external-facing documents
- **`internal`** — contains phrases like "confidential" or "internal use only"
- **`confidential`** — board/executive materials (requires explicit override at ingestion time)

## HITL Threshold Behavior

When `requires_hitl_above` is set and the generated answer references a dollar amount above the threshold, the graph pauses via LangGraph's `interrupt()`. The caller must resume with `POST /hitl/approve` or `/hitl/reject`.

Amount extraction regex ([src/graph/nodes/hitl_gate.py](../src/graph/nodes/hitl_gate.py)) handles:
- `$100,000`, `$100k`, `$500K`
- `$2.5 million`, `$1.5M`
- `$383.3 billion`, `$2B`, `$1T`

## Role Fallback

Unknown or missing roles fall back to `analyst` (most restrictive). This is a fail-safe default — if a JWT is malformed or a new role is introduced without config, the user gets minimum access rather than maximum.

## How Enforcement Works

1. `rbac_gate` node extracts `user_role` from state and looks up `allowed_doc_types` + `allowed_confidentiality`.
2. `retrieval` node calls `build_rbac_filter()` ([src/services/vector_store.py](../src/services/vector_store.py)), which builds a Qdrant `Filter` with two `FieldCondition` clauses:
   - `doc_type` ∈ allowed types
   - `confidentiality` ∈ allowed levels
3. Qdrant's HNSW search only considers points matching the filter. Points for disallowed docs are never read from disk.
4. `admin` users get `"*"` wildcards, which skip filter construction (no restriction).

## Test Accounts

For development, [src/api/routes/auth.py](../src/api/routes/auth.py) defines in-memory users:

| Username | Password | Role |
|----------|----------|------|
| `analyst` | `analyst123` | analyst |
| `finance` | `finance123` | finance |
| `hr` | `hr123` | hr |
| `clevel` | `clevel123` | c_level |
| `admin` | `admin123` | admin |

In production, replace `DEV_USERS` with an actual user store (LDAP, IdP, database).

## Testing Cross-Role Leakage

To verify RBAC works, try asking an `hr` user for Apple's revenue. The 10-K is not in their allowed doc types, so retrieval returns zero chunks and the system answers: *"I couldn't find relevant information in the available documents to answer your question."* Tests for this live in [tests/unit/test_rbac_gate.py](../tests/unit/test_rbac_gate.py).
