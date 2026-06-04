"""Emit per-diagram spec markdown for the FinanceBench RAG Agent.

Internal tooling. Each .md hands a downstream Excalidraw-capable AI everything it
needs: verified code refs, a node/edge layout table, an importable .excalidraw JSON
scaffold (hand-drawn style, light palette, text positioned), and a check list.
Diagram CONTENT is grounded in the audited architecture — do not invent flow.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[3] / "docs" / "diagrams" / "excalidraw"
STAMP = 1717459200000

PALETTE = {
    "client": ("#e7f5ff", "#1971c2"),
    "access": ("#ede0ff", "#7048e8"),
    "safety": ("#fff3bf", "#f08c00"),
    "retr":   ("#d0ebff", "#1c7ed6"),
    "gen":    ("#d3f9d8", "#2f9e44"),
    "warn":   ("#ffe8cc", "#e8590c"),
    "crit":   ("#ffe3e3", "#e03131"),
    "store":  ("#c5f6fa", "#0c8599"),
    "none":   ("#f1f3f5", "#868e96"),
}
INK = "#1e1e1e"


class Scene:
    def __init__(self):
        self.els: list[dict] = []
        self._n = 0
        self.nodes: dict[str, dict] = {}

    def _id(self, p="el"):
        self._n += 1
        return f"{p}{self._n:03d}"

    def _base(self, **kw):
        d = dict(
            angle=0, strokeColor=INK, backgroundColor="transparent", fillStyle="solid",
            strokeWidth=2, strokeStyle="solid", roughness=1, opacity=100, groupIds=[],
            frameId=None, roundness={"type": 3}, seed=self._n * 7919 + 13, version=1,
            versionNonce=self._n * 104729 + 7, isDeleted=False, boundElements=[],
            updated=STAMP, link=None, locked=False,
        )
        d.update(kw)
        return d

    def _label(self, cid, text, center, color=INK, size=16):
        lines = text.split("\n")
        w = max(len(ln) for ln in lines) * size * 0.6
        h = len(lines) * size * 1.25
        cx, cy = center
        t = self._base(
            id=self._id("t"), type="text", x=cx - w / 2, y=cy - h / 2, width=w, height=h,
            strokeColor=color, backgroundColor="transparent", roundness=None,
            text=text, originalText=text, fontSize=size, fontFamily=1,
            textAlign="center", verticalAlign="middle", containerId=cid,
            lineHeight=1.25, autoResize=True,
        )
        self.els.append(t)
        return t["id"]

    def node(self, key, cx, cy, w, h, text, kind="none", shape="rectangle", font=16):
        fill, stroke = PALETTE[kind]
        el = self._base(
            id=self._id("n"), type=shape, x=cx - w / 2, y=cy - h / 2, width=w, height=h,
            backgroundColor=fill, strokeColor=stroke,
            roundness={"type": 3} if shape == "rectangle" else None,
        )
        self.els.append(el)
        tid = self._label(el["id"], text, (cx, cy), INK, font)
        el["boundElements"] = [{"type": "text", "id": tid}]
        self.nodes[key] = dict(id=el["id"], cx=cx, cy=cy, w=w, h=h, kind=kind,
                               shape=shape, text=text)

    def _port(self, key, side):
        n = self.nodes[key]
        cx, cy, w, h = n["cx"], n["cy"], n["w"], n["h"]
        return {"top": (cx, cy - h / 2), "bottom": (cx, cy + h / 2),
                "left": (cx - w / 2, cy), "right": (cx + w / 2, cy)}[side]

    def edge(self, a, asd, b, bsd, label="", dashed=False, two_way=False):
        ax, ay = self._port(a, asd)
        bx, by = self._port(b, bsd)
        arr = self._base(
            id=self._id("a"), type="arrow", x=ax, y=ay,
            width=abs(bx - ax), height=abs(by - ay), backgroundColor="transparent",
            roundness={"type": 2}, strokeStyle="dashed" if dashed else "solid",
            points=[[0.0, 0.0], [round(bx - ax, 2), round(by - ay, 2)]],
            lastCommittedPoint=None,
            startBinding={"elementId": self.nodes[a]["id"], "focus": 0, "gap": 6},
            endBinding={"elementId": self.nodes[b]["id"], "focus": 0, "gap": 6},
            startArrowhead="arrow" if two_way else None, endArrowhead="arrow",
        )
        self.els.append(arr)
        for el in self.els:
            if el["id"] in (self.nodes[a]["id"], self.nodes[b]["id"]):
                el["boundElements"] = el.get("boundElements", []) + [
                    {"type": "arrow", "id": arr["id"]}]
        if label:
            lid = self._label(arr["id"], label, ((ax + bx) / 2, (ay + by) / 2), INK, 14)
            arr["boundElements"] = [{"type": "text", "id": lid}]

    def title(self, text, x=60, y=24, size=28):
        self.els.append(self._base(
            id=self._id("ti"), type="text", x=x, y=y, width=len(text) * size * 0.55,
            height=size * 1.25, roundness=None, backgroundColor="transparent",
            text=text, originalText=text, fontSize=size, fontFamily=1, textAlign="left",
            verticalAlign="top", containerId=None, lineHeight=1.25, autoResize=True))

    def note(self, text, x, y, size=14):
        self.els.append(self._base(
            id=self._id("no"), type="text", x=x, y=y, width=len(text) * size * 0.55,
            height=size * 1.25, roundness=None, backgroundColor="transparent",
            strokeColor="#495057", text=text, originalText=text, fontSize=size,
            fontFamily=1, textAlign="left", verticalAlign="top", containerId=None,
            lineHeight=1.25, autoResize=True))

    def to_dict(self):
        return {"type": "excalidraw", "version": 2, "source": "https://excalidraw.com",
                "elements": self.els,
                "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None},
                "files": {}}


def build(spec) -> Scene:
    s = Scene()
    s.title(spec["title"])
    for n in spec["nodes"]:
        s.node(*n)
    for e in spec["edges"]:
        s.edge(*e)
    for nt in spec.get("notes", []):
        s.note(*nt)
    return s


# node tuple: (key, cx, cy, w, h, text, kind, shape[, font])
# edge tuple: (a, a_side, b, b_side, label, dashed, two_way)
DIAGRAMS = {
"01-architecture-hero": {
  "title": "FinanceBench RAG Agent — request pipeline",
  "nodes": [
    ("cli", 360, 110, 200, 64, "CLI\n(financebench)", "client"),
    ("api", 690, 110, 290, 64, "FastAPI\nJWT auth · SSE stream", "client"),
    ("rbac", 530, 250, 320, 74, "rbac_gate\nrole -> doc filter", "access"),
    ("guard", 530, 372, 380, 80, "guardrails\nPII · 3-layer injection · memory rewrite", "safety", "rectangle", 15),
    ("router", 530, 520, 250, 120, "router\nintent +\ncomplexity", "safety", "diamond"),
    ("retr", 320, 700, 330, 96, "Retrieve · Rerank · Grade\nhybrid -> BGE reranker\n-> LLM grader", "retr", "rectangle", 15),
    ("agent", 780, 700, 330, 96, "research_agent subgraph\ndecompose -> sufficiency\n-> synthesize", "access", "rectangle", 15),
    ("gen", 530, 850, 330, 74, "generator\nClaude Sonnet 4.6 (cached)", "gen"),
    ("hall", 530, 972, 380, 80, "hallucination check\nClaude Sonnet 4.6", "warn", "rectangle", 15),
    ("hitl", 530, 1120, 270, 120, "hitl_gate\namount > role\nthreshold?", "crit", "diamond"),
    ("resp", 330, 1300, 320, 74, "response_formatter\n-> SSE", "gen"),
    ("blocked", 780, 1300, 220, 64, "blocked", "crit"),
    ("qdrant", 1190, 670, 260, 110, "Qdrant\ndense+BM25 vectors\nRBAC payload filter", "store", "rectangle", 15),
    ("redis", 1190, 830, 260, 80, "Redis\nresult cache", "store"),
    ("pg", 1190, 1030, 260, 110, "PostgreSQL\nHITL checkpoints\n+ thread memory", "store", "rectangle", 15),
  ],
  "edges": [
    ("cli", "right", "api", "left", "", False, False),
    ("api", "bottom", "rbac", "top", "", False, False),
    ("rbac", "bottom", "guard", "top", "", False, False),
    ("guard", "bottom", "router", "top", "", False, False),
    ("router", "left", "retr", "top", "simple", False, False),
    ("router", "right", "agent", "top", "research", False, False),
    ("retr", "bottom", "gen", "top", "", False, False),
    ("agent", "bottom", "gen", "top", "", False, False),
    ("gen", "bottom", "hall", "top", "", False, False),
    ("hall", "bottom", "hitl", "top", "", False, False),
    ("hitl", "left", "resp", "top", "approved / n a", False, False),
    ("hitl", "right", "blocked", "top", "rejected", False, False),
    ("retr", "right", "qdrant", "left", "RBAC filter", True, True),
    ("retr", "right", "redis", "left", "cache", True, True),
    ("guard", "right", "pg", "top", "thread memory", True, True),
    ("hitl", "right", "pg", "left", "interrupt + ckpt", True, True),
  ],
  "notes": [("Simple lookups take the fast path; research queries enter the subgraph. RBAC is enforced at the Qdrant payload-filter level.", 60, 1410)],
  "published": "README + PyPI (hero image, replaces docs/diagrams/architecture.png)",
  "type": "System architecture / data-flow (top-to-bottom pipeline + right-hand datastore column)",
  "refs": [
    ("src/graph/builder.py", "L31-119 — StateGraph: 18 add_node calls (L37-54) + edge wiring (L57-116)"),
    ("src/graph/edges.py", "L7-90 — all conditional routers (guardrails, router, retrieval_evaluator, grader, hallucination, hitl)"),
    ("src/graph/nodes/generator.py", "L120,167 — generator_node, get_generator_llm() -> claude-sonnet-4-6"),
    ("src/graph/nodes/hallucination.py", "L60,81-83 — get_hallucination_llm / get_high_stakes_hallucination_llm; both default claude-sonnet-4-6 (settings.py:160,165)"),
    ("src/graph/nodes/research_agent.py", "L433 — research_agent_node (subgraph entry)"),
    ("src/api/main.py", "L73-84 — AsyncPostgresSaver checkpointer (HITL + memory persistence)"),
  ],
  "prepare": "This is the headline. 18 LangGraph nodes total; the diagram shows the 12 on the main spine + the 3 datastores. Terminal-only nodes (blocked_response, out_of_scope_response, clarification_response, no_info_response) and the retry-helper nodes (retrieval_evaluator, query_rewriter, entity_extractor) are collapsed into the boxes shown to keep the hero legible — name that collapse in the figure caption, do not imply only 12 nodes exist.",
  "checklist": [
    "Says 18-node LangGraph (matches builder.py add_node count); does NOT show only the 12 spine boxes as the full count.",
    "generator = Claude Sonnet 4.6 (NOT gpt-4o-mini); hallucination check = Claude Sonnet 4.6 (settings.py:160; high-stakes path also Sonnet 4.6 at L165, configurable to Opus). NOT Haiku.",
    "RBAC filter arrow lands on Qdrant (storage layer), not on the API box.",
    "research vs simple branch both converge into the generator.",
    "No sprint numbers, no trajectory/journey language, no emojis (README public-surface rules).",
  ],
},

"02-rbac": {
  "title": "Role-based access control — enforced at the storage layer",
  "nodes": [
    ("analyst", 240, 220, 320, 84, "analyst\n10-K · public\nno HITL", "access", "rectangle", 14),
    ("finance", 240, 330, 320, 84, "finance\n10-K, invoice, expense_policy\npublic, internal · HITL > $100K", "access", "rectangle", 13),
    ("hr", 240, 440, 320, 84, "hr\nexpense_policy\npublic, internal · no HITL", "access", "rectangle", 14),
    ("clevel", 240, 550, 320, 84, "c_level\n+ board_report · + confidential\nHITL > $1M", "access", "rectangle", 13),
    ("admin", 240, 660, 320, 84, "admin\n* / *\nno HITL", "access", "rectangle", 14),
    ("filter", 720, 440, 320, 150, "build_retrieval_filter()\nQdrant payload filter\ndoc_type & confidentiality\n& company & fiscal_year", "safety", "rectangle", 14),
    ("corpus", 1180, 330, 300, 120, "Qdrant corpus\npayload: doc_type,\nconfidentiality, company,\nfiscal_year", "store", "rectangle", 14),
    ("allowed", 1180, 540, 300, 90, "only chunks this\nrole may see", "gen"),
    ("approval", 720, 700, 360, 130, "can_approve()\nc_level -> finance / hr / analyst\nadmin -> *\nself-approval blocked", "crit", "rectangle", 14),
  ],
  "edges": [
    ("analyst", "right", "filter", "left", "", False, False),
    ("finance", "right", "filter", "left", "", False, False),
    ("hr", "right", "filter", "left", "", False, False),
    ("clevel", "right", "filter", "left", "", False, False),
    ("admin", "right", "filter", "left", "", False, False),
    ("filter", "right", "corpus", "left", "query-time filter", False, False),
    ("corpus", "bottom", "allowed", "top", "matched", False, False),
  ],
  "notes": [("RBAC is applied inside the Qdrant query, so agentic / research-agent queries cannot bypass it.", 60, 840),
            ("HITL approval (right) is a separate axis: who may approve a paused high-stakes answer.", 60, 870)],
  "published": "Medium (RBAC deep-dive)",
  "type": "Permission matrix + enforcement-point flow (roles -> filter -> corpus) with a separate approval-authority panel",
  "refs": [
    ("src/config/rbac_config.py", "L1-32 — ROLE_PERMISSIONS (allowed_doc_types, allowed_confidentiality, max_results, requires_hitl_above)"),
    ("src/config/rbac_config.py", "L64-70 — CAN_APPROVE_FOR; L73-79 — can_approve() (self-approval returns False at L77)"),
    ("src/services/vector_store.py", "L195-221 — build_retrieval_filter() builds FieldConditions on doc_type/confidentiality/company/fiscal_year"),
    ("src/services/vector_store.py", "L243-270 — filter passed into Prefetch + FusionQuery (applied at query time)"),
    ("src/graph/nodes/retrieval.py", "L70-75 — retrieval_node passes the RBAC filter into hybrid_search"),
  ],
  "prepare": "Thresholds are exact, quote them verbatim: finance $100,000; c_level $1,000,000; analyst/hr/admin = None. max_results per role: analyst 5, finance 10, hr 5, c_level 15, admin 20 (add if room). The '*' for admin means all doc_types/confidentiality.",
  "checklist": [
    "finance threshold = $100K, c_level = $1M, others None — exact.",
    "Enforcement point is the Qdrant payload filter (storage), not API middleware.",
    "Self-approval shown as blocked; c_level can approve finance/hr/analyst but not itself.",
    "doc_type values match code: 10k, invoice, expense_policy, board_report.",
  ],
},

"03-hitl": {
  "title": "Human-in-the-loop — multi-party approval across sessions",
  "nodes": [
    ("u1", 300, 180, 300, 80, "finance user asks a\nhigh-stakes question", "client"),
    ("ans", 300, 300, 300, 80, "generator answer\nreferences $200,000", "gen"),
    ("gate", 300, 450, 320, 130, "hitl_gate\n$200K > $100K\nrole threshold?", "crit", "diamond"),
    ("ckpt", 720, 470, 280, 120, "PostgreSQL\ninterrupt() + checkpoint\npending approval persisted", "store", "rectangle", 14),
    ("pending", 1120, 300, 300, 90, "approval queue\nvisible to approvers", "warn"),
    ("approver", 1120, 460, 300, 100, "admin / c_level\ncan_approve()?\nself-approval blocked", "access", "rectangle", 14),
    ("resume", 720, 720, 280, 90, "graph resumes\nfrom checkpoint", "gen"),
    ("released", 300, 720, 300, 80, "answer released\nto requester", "gen"),
  ],
  "edges": [
    ("u1", "bottom", "ans", "top", "", False, False),
    ("ans", "bottom", "gate", "top", "", False, False),
    ("gate", "right", "ckpt", "left", "> threshold", False, False),
    ("ckpt", "right", "pending", "left", "", False, False),
    ("pending", "bottom", "approver", "top", "", False, False),
    ("approver", "left", "resume", "right", "approve", False, False),
    ("ckpt", "bottom", "resume", "top", "on decision", False, False),
    ("resume", "left", "released", "right", "", False, False),
    ("gate", "bottom", "released", "left", "<= threshold (auto)", True, False),
  ],
  "notes": [("State is checkpointed to Postgres, so the pause survives container restarts and the approver can act in a different terminal / session.", 60, 840)],
  "published": "Medium (HITL deep-dive)",
  "type": "Two-party sequence / state-machine (requester lane | Postgres checkpoint | approver lane)",
  "refs": [
    ("src/graph/nodes/hitl_gate.py", "L48-134 — hitl_gate_node; L62-63 role threshold lookup; L76 compare; L93 interrupt()"),
    ("src/graph/nodes/hitl_gate.py", "L12-45 — _extract_max_amount (takes max of query+answer); L20 _SANE_MAX_AMOUNT=1e13 noise guard"),
    ("src/config/rbac_config.py", "L73-79 — can_approve(); self-approval blocked at L77"),
    ("src/api/main.py", "L73-84 — AsyncPostgresSaver persists the interrupted state"),
    ("src/graph/edges.py", "L79-90 — route_after_hitl: no_approval_needed / approved / rejected"),
  ],
  "prepare": "The gate trips when max(amount-in-query, amount-in-answer) > the requester role's threshold. Use a concrete example: finance user, $200K, exceeds $100K -> interrupt. Show that approval is multi-party (a DIFFERENT user with can_approve authority) and that self-approval is rejected.",
  "checklist": [
    "Trigger condition is amount > role threshold (finance $100K) — uses the MAX of query and answer amounts.",
    "interrupt() + Postgres checkpoint shown as the persistence mechanism (survives restart).",
    "Approver is a distinct party; self-approval blocked.",
    "rejected path goes to blocked_response (not a silent drop).",
  ],
},

"04-memory": {
  "title": "Conversation memory — follow-up query rewriting",
  "nodes": [
    ("t1", 300, 200, 330, 80, "Turn 1: 'Apple FY23 revenue?'", "client"),
    ("store", 300, 330, 330, 90, "PostgreSQL checkpoint\nthread messages (add_messages)", "store", "rectangle", 14),
    ("t2", 300, 490, 330, 80, "Turn 2: 'And Microsoft?'", "client"),
    ("hist", 740, 490, 330, 100, "_format_history()\nlast 3 turns / 6 msgs\nAI replies truncated 400 chars", "safety", "rectangle", 14),
    ("llm", 740, 650, 330, 90, "router LLM (Llama-3.3-70B)\nQUERY_CONTEXTUALIZER_PROMPT", "access", "rectangle", 14),
    ("rewritten", 300, 650, 330, 80, "rewritten:\n'Microsoft FY23 revenue?'", "gen"),
    ("pipe", 300, 790, 330, 70, "-> standalone query into pipeline", "gen", "rectangle", 14),
  ],
  "edges": [
    ("t1", "bottom", "store", "top", "", False, False),
    ("store", "bottom", "t2", "top", "", False, False),
    ("t2", "right", "hist", "left", "", False, False),
    ("hist", "bottom", "llm", "top", "", False, False),
    ("llm", "left", "rewritten", "right", "", False, False),
    ("rewritten", "bottom", "pipe", "top", "", False, False),
    ("store", "right", "hist", "top", "prior turns", True, False),
  ],
  "notes": [("Contextualization runs inside the guardrails node, before routing — so the rewritten standalone query is what RBAC, retrieval, and the router all see.", 60, 880)],
  "published": "Medium (conversation memory deep-dive)",
  "type": "Two-turn sequence showing state read-back + LLM rewrite",
  "refs": [
    ("src/graph/nodes/guardrails.py", "L18-32 — _format_history (last 3 turns / 6 msgs, AI truncated to 400 chars)"),
    ("src/graph/nodes/guardrails.py", "L35-59 — _contextualize_query; L49 get_router_llm(); L50 QUERY_CONTEXTUALIZER_PROMPT"),
    ("src/config/prompts.py", "QUERY_CONTEXTUALIZER_PROMPT template"),
    ("src/models/state.py", "L11 — messages: Annotated[list, add_messages] (thread history)"),
    ("src/api/main.py", "L81 — AsyncPostgresSaver persists thread state across turns/sessions"),
    ("src/services/llm_factory.py", "L204-218 — get_router_llm (Groq llama-3.3-70b-versatile, OpenAI fallback)"),
  ],
  "prepare": "Memory is not a separate vector store — it is the LangGraph message history persisted in the Postgres checkpointer, plus an LLM rewrite step that resolves coreferences ('And Microsoft?' -> 'Microsoft FY23 revenue?'). The rewrite uses the router-tier model, not the generator.",
  "checklist": [
    "Rewrite model = router LLM (Llama-3.3-70B), not Claude.",
    "History window = last 3 turns / 6 messages, AI replies truncated to 400 chars.",
    "Storage = Postgres checkpointer (same mechanism as HITL), not a bespoke memory DB.",
    "Rewrite happens before routing/retrieval (inside guardrails node).",
  ],
},

"05-retrieval-rerank-grade": {
  "title": "Retrieval -> rerank -> grade",
  "nodes": [
    ("q", 400, 140, 300, 70, "sanitized query", "client"),
    ("embed", 400, 260, 360, 80, "embed\nvoyage-finance-2 /\ntext-embedding-3-small (1536-d)", "retr", "rectangle", 14),
    ("hybrid", 400, 410, 380, 100, "Qdrant hybrid search\ndense + BM25 sparse · RRF k=60\nRBAC payload filter applied", "retr", "rectangle", 14),
    ("top50", 800, 410, 150, 64, "top 50", "none"),
    ("rerank", 400, 570, 380, 90, "BGE-reranker-v2-m3\ncross-encoder", "retr"),
    ("top8", 800, 570, 150, 64, "top 8", "none"),
    ("g1", 400, 720, 380, 64, "Stage 1 · entity_match (deterministic)", "safety", "rectangle", 14),
    ("g2", 400, 810, 380, 64, "Stage 2 · LTR gate (optional)", "safety", "rectangle", 14),
    ("g3", 400, 910, 380, 90, "Stage 3 · LLM relevance\n8-way parallel · Llama-3.3-70B", "safety", "rectangle", 14),
    ("decision", 400, 1080, 300, 120, "enough\nrelevant\nchunks?", "crit", "diamond"),
    ("gen", 400, 1270, 300, 70, "-> generator", "gen"),
    ("rewrite", 860, 1080, 280, 90, "query_rewriter\nretry loop (bounded)", "warn", "rectangle", 14),
  ],
  "edges": [
    ("q", "bottom", "embed", "top", "", False, False),
    ("embed", "bottom", "hybrid", "top", "", False, False),
    ("hybrid", "right", "top50", "left", "", False, False),
    ("hybrid", "bottom", "rerank", "top", "", False, False),
    ("rerank", "right", "top8", "left", "", False, False),
    ("rerank", "bottom", "g1", "top", "", False, False),
    ("g1", "bottom", "g2", "top", "", False, False),
    ("g2", "bottom", "g3", "top", "", False, False),
    ("g3", "bottom", "decision", "top", "", False, False),
    ("decision", "bottom", "gen", "top", "sufficient", False, False),
    ("decision", "right", "rewrite", "left", "retry", False, False),
    ("rewrite", "top", "hybrid", "right", "re-retrieve", True, False),
  ],
  "notes": [("Pool of 50 is reranked to 8; the grader then escalates from a free deterministic check to an optional LTR gate to parallel LLM scoring.", 60, 1380)],
  "published": "Medium (retrieval pipeline deep-dive — includes reranker + grader)",
  "type": "Vertical pipeline with fan-out counts and a bounded retry loop",
  "refs": [
    ("src/graph/nodes/retrieval.py", "L16 _RRF_K=60; embeds + hybrid search; settings RETRIEVAL_TOP_K=50"),
    ("src/services/vector_store.py", "L39-41 dense/sparse/Qdrant-bm25 names; L224-271 RRF fusion (Fusion.RRF L259)"),
    ("src/config/settings.py", "L96 RETRIEVAL_TOP_K=50; L98 RERANKER_TOP_K=8; L142-144 embedding provider/model/dim"),
    ("src/services/reranker_service.py", "L34 BAAI/bge-reranker-v2-m3; L92-98 RERANKER_ADAPTER_PATH + silent stock fallback"),
    ("src/graph/nodes/grader.py", "L28-43 _entity_match; L82-133 LTR gate; L151-184 8-way parallel LLM; L25 GRADER_PARALLELISM=8"),
    ("src/graph/edges.py", "L43-52 route_after_grading; L55-62 route_after_retrieval_evaluator (retry loop)"),
  ],
  "prepare": "Production uses STOCK BAAI/bge-reranker-v2-m3 (no LoRA adapter active). Mention the LoRA adapter hook (RERANKER_ADAPTER_PATH) only as an option, not as live, or omit it. Embedding: voyage-finance-2 is canonical, text-embedding-3-small is the default/fallback. Numbers exact: 50 -> 8, RRF k=60, grader parallelism 8.",
  "checklist": [
    "Reranker is stock bge-reranker-v2-m3 (do NOT depict a fine-tuned reranker as production — both LoRA variants were rolled back).",
    "RRF k=60, retrieve top-50, rerank top-8, grader 8-way parallel — exact.",
    "Grader stages in order: entity_match (deterministic) -> LTR gate (optional) -> LLM relevance.",
    "RBAC filter applied during the Qdrant query (consistent with the RBAC diagram).",
  ],
},

"06-research-agent": {
  "title": "Research-agent subgraph — selective decomposition",
  "nodes": [
    ("router", 420, 150, 300, 110, "router\ncomplexity?", "safety", "diamond"),
    ("simple", 120, 320, 240, 80, "simple ->\ndirect retrieval path", "none", "rectangle", 14),
    ("decompose", 520, 320, 340, 90, "_decompose()\ngpt-4o-mini\n2-4 sub-questions (max 5)", "access", "rectangle", 14),
    ("subq", 520, 460, 360, 90, "per sub-question:\nretrieve + rerank + grade", "retr", "rectangle", 14),
    ("dedup", 520, 590, 360, 64, "dedup chunks by id", "none", "rectangle", 14),
    ("suff", 520, 730, 330, 130, "_judge_sufficiency()\nenough?", "crit", "diamond"),
    ("followup", 960, 730, 280, 90, "follow-up sub-question\n(<= 2 rounds)", "warn", "rectangle", 14),
    ("synth", 520, 920, 360, 90, "_synthesize()\nmarkdown synthesis", "gen", "rectangle", 14),
    ("gen", 520, 1060, 320, 70, "-> generator (agent_synthesis)", "gen", "rectangle", 14),
  ],
  "edges": [
    ("router", "left", "simple", "top", "simple_lookup", False, False),
    ("router", "bottom", "decompose", "top", "research_required", False, False),
    ("decompose", "bottom", "subq", "top", "", False, False),
    ("subq", "bottom", "dedup", "top", "", False, False),
    ("dedup", "bottom", "suff", "top", "", False, False),
    ("suff", "right", "followup", "left", "need_more", False, False),
    ("followup", "top", "subq", "right", "retrieve again", True, False),
    ("suff", "bottom", "synth", "top", "sufficient", False, False),
    ("synth", "bottom", "gen", "top", "", False, False),
  ],
  "notes": [("Turn budget: decompose (1) + <= 2 sufficiency rounds + synthesize (1) = <= 5 LLM turns. Only research-flagged queries enter here; lookups skip it.", 60, 1140)],
  "published": "Medium (the selective-agentic differentiator)",
  "type": "Subgraph flow with a bounded sufficiency loop",
  "refs": [
    ("src/graph/nodes/research_agent.py", "L433-532 research_agent_node; L62 MAX_LLM_TURNS=5; L63 MAX_SUB_QUESTIONS=5; L64 MAX_FOLLOWUP_ROUNDS=2"),
    ("src/graph/nodes/research_agent.py", "L227-255 _decompose (get_research_decompose_llm); L258-293 _judge_sufficiency; L305-363 _synthesize"),
    ("src/graph/nodes/research_agent.py", "L192-219 _retrieve_and_grade_for_subq (reuses retrieval/reranker/grader nodes)"),
    ("src/graph/builder.py", "L49 add_node('research_agent'); L84 add_edge('research_agent','generator')"),
    ("src/graph/edges.py", "L15-40 route_after_router (research_required vs retrieval)"),
  ],
  "prepare": "The decompose/sufficiency/synthesize LLMs are the gpt-4o-mini research tier (get_research_* in llm_factory), NOT the Sonnet generator. Sub-questions capped at 5, follow-up rounds capped at 2, total turns <= 5. The subgraph reuses the SAME retrieval/reranker/grader nodes per sub-question — show that reuse.",
  "checklist": [
    "Sub-questions max 5, follow-up rounds max 2, total LLM turns <= 5 — exact constants.",
    "Per-sub-question step reuses retrieval+reranker+grader (not a separate retriever).",
    "decompose + sufficiency = gpt-4o-mini (settings.py:170-171); synthesize = claude-sonnet-4-6 (settings.py:172); final generation downstream also Sonnet 4.6.",
    "Entry is gated by router 'research_required'; simple lookups bypass.",
  ],
},

"07-guardrails": {
  "title": "Guardrails cascade — PII + 3-layer injection defense",
  "nodes": [
    ("q", 400, 140, 300, 70, "user query", "client"),
    ("pii", 400, 270, 380, 90, "PII detection (Presidio)\nPERSON, EMAIL, SSN, CARD...\nredacted in place", "safety", "rectangle", 14),
    ("l1", 400, 420, 380, 64, "Layer 1 · regex (8 patterns, ~0 ms)", "safety", "rectangle", 14),
    ("l2", 400, 520, 380, 80, "Layer 2 · LLM Guard ONNX\nPromptInjection threshold 0.9 (~100 ms)", "safety", "rectangle", 13),
    ("l3", 400, 650, 380, 90, "Layer 3 · LLM classifier\nrouter LLM, conf >= 0.7 (~1-2 s)\nonly if risk 0.5-0.9", "safety", "rectangle", 13),
    ("clean", 400, 810, 360, 80, "clean -> contextualize\n-> entity_extractor", "gen", "rectangle", 14),
    ("blocked", 880, 540, 260, 90, "blocked_response\ninjection detected", "crit", "rectangle", 14),
  ],
  "edges": [
    ("q", "bottom", "pii", "top", "", False, False),
    ("pii", "bottom", "l1", "top", "", False, False),
    ("l1", "bottom", "l2", "top", "pass", False, False),
    ("l2", "bottom", "l3", "top", "risk 0.5-0.9", False, False),
    ("l3", "bottom", "clean", "top", "clean", False, False),
    ("l1", "right", "blocked", "left", "match", True, False),
    ("l2", "right", "blocked", "left", "injection", True, False),
    ("l3", "right", "blocked", "left", "injection", True, False),
  ],
  "notes": [("Layers escalate in cost/latency; most queries exit cheaply at Layer 1 or pass straight through. Layer 3 only fires for borderline LLM-Guard scores.", 60, 900)],
  "published": "Medium (safety/guardrails deep-dive)",
  "type": "Escalating-cost cascade with an early-exit block branch",
  "refs": [
    ("src/services/guardrails_service.py", "L7-25 regex (8 patterns); L28-88 LLM Guard ONNX (threshold 0.9); L90-120 LLM classifier"),
    ("src/services/guardrails_service.py", "L123-242 Presidio PII (PERSON, PHONE, EMAIL, CREDIT_CARD, US_SSN, US_BANK_NUMBER, IBAN, IP)"),
    ("src/graph/nodes/guardrails.py", "L62-113 guardrails_node; L88 L1; L93 L2; L100-102 L3 trigger (risk>=0.5, conf>=0.7)"),
    ("src/graph/edges.py", "L7-12 route_after_guardrails (clean vs blocked)"),
  ],
  "prepare": "Order matters: PII redaction runs first (always), then the 3 injection layers in increasing cost. Layer 3 is conditional — only when LLM-Guard risk is 0.5-0.9. Quote the thresholds: LLM Guard 0.9, Layer-3 trigger risk>=0.5, Layer-3 confidence>=0.7.",
  "checklist": [
    "PII (Presidio) runs first and is always-on; injection layers follow.",
    "Layer order: regex -> LLM Guard (ONNX, 0.9) -> LLM classifier (conditional, risk 0.5-0.9, conf 0.7).",
    "Layer 3 is conditional, not always-run — show the dashed/conditional entry.",
    "Block branch routes to blocked_response.",
  ],
},

"08-eval-methodology": {
  "title": "FinanceBench evaluation — how 72.7% is measured",
  "nodes": [
    ("corpus", 420, 140, 340, 80, "FinanceBench-150\n32 companies · 68k chunks", "store", "rectangle", 14),
    ("pipe", 420, 270, 360, 90, "18-node pipeline\ncollection:\n..._pypdf_voyage_finance2", "retr", "rectangle", 14),
    ("cache", 420, 420, 360, 90, ".pipeline.json cache\n18-field reproducibility snapshot", "none", "rectangle", 14),
    ("ragas", 150, 580, 280, 100, "RAGAS (gpt-4o-mini)\nfaith / relevancy /\nctx prec + recall", "warn", "rectangle", 14),
    ("deepeval", 460, 580, 280, 100, "DeepEval (gpt-4o-mini)\nfaith / ctx recall + prec", "warn", "rectangle", 14),
    ("judge", 780, 580, 320, 130, "Correctness judge\nClaude Sonnet 4.6 + IMPROVED_PROMPT v2\nκ=0.932 vs 89-Q calib + 15-Q holdout", "crit", "rectangle", 13),
    ("headline", 460, 770, 380, 120, "72.7% pass (109/150)\nadjusted-actionable 77.3%\nlookup 68.6 · multi-hop 84.6 · calc 76.5", "gen", "rectangle", 13),
    ("banner", 850, 780, 300, 90, "Tier-1 boot banner\n(event_log.py) provenance", "store", "rectangle", 14),
  ],
  "edges": [
    ("corpus", "bottom", "pipe", "top", "", False, False),
    ("pipe", "bottom", "cache", "top", "", False, False),
    ("cache", "bottom", "ragas", "top", "", False, False),
    ("cache", "bottom", "deepeval", "top", "", False, False),
    ("cache", "bottom", "judge", "top", "", False, False),
    ("judge", "bottom", "headline", "top", "", False, False),
    ("deepeval", "bottom", "headline", "top", "", False, False),
  ],
  "notes": [("rejudge.py re-scores any cached *.correctness.json against the calibrated judge in ~3 min. Three judges run in parallel; the correctness judge is the headline gate.", 60, 920),
            ("Numbers are from docs/evaluation.md (authoritative). Do NOT use older eval_results files (e.g. *_calc.json at 40.67%, judge_eval_v1.json at κ=0.8613) — those are pre-recalibration / rolled-back configs.", 60, 950)],
  "published": "Medium (evaluation methodology) + supports README evaluation section",
  "type": "Pipeline -> cache -> parallel-judge fan-out -> headline",
  "refs": [
    ("docs/evaluation.md", "L1 + methodology section — AUTHORITATIVE source for all numbers (72.7%, κ=0.932, 89-Q calib + 15-Q holdout, adjusted 77.3%)"),
    ("tests/evaluation/run_financebench.py", "pipeline phase + RAGAS/DeepEval/correctness scoring; --collection override"),
    ("tests/evaluation/judge_eval.py", "L66-96 IMPROVED_PROMPT; L339 _make_anthropic_judge('claude-sonnet-4-6')"),
    ("tests/evaluation/rejudge.py", "re-scores existing *.correctness.json against the calibrated judge"),
    ("src/services/event_log.py", "L121-382 log_runtime_components (Tier-1 boot banner; provenance for every run)"),
  ],
  "prepare": "CREDIBILITY: source every number from docs/evaluation.md, not from raw eval_results/*.json (many are stale experiments). Headline: 72.7% (109/150), adjusted-actionable 77.3% (excludes 9 FB dataset errors), κ=0.932. Per-slice (from README): lookup 68.6% n=86, multi-hop 84.6% n=13, calc 76.5% n=51. RAGAS faith 0.747, DeepEval faith 0.844, DeepEval ctx recall 0.768. The correctness judge is the gate; RAGAS/DeepEval are secondary signals on gpt-4o-mini.",
  "checklist": [
    "Headline 72.7% (109/150), κ=0.932 — matches docs/evaluation.md and README exactly.",
    "Correctness judge = Claude Sonnet 4.6 + IMPROVED_PROMPT v2 (the gate); RAGAS/DeepEval = gpt-4o-mini secondary.",
    "Calibration = 89-Q hand-labeled + 15-Q holdout; prior gpt-4o-mini judge was κ=0.490 (mention as the 'why recalibrate').",
    "Do NOT show 40.67% or κ=0.8613 — those are stale/rolled-back files.",
    "No sprint numbers if this feeds the README; Medium version may reference the methodology narrative.",
  ],
},
}


def md_for(name, spec, scene_dict) -> str:
    L = []
    L.append(f"# Diagram spec — {spec['title']}\n")
    L.append(f"**File:** `{name}.md`  ·  **Publish target:** {spec['published']}\n")
    L.append(f"**Diagram type:** {spec['type']}\n")
    L.append("> Generated scaffold. The flow content is grounded in the audited "
             "codebase (refs below) — a downstream Excalidraw-capable agent should "
             "refine the *visual* layout, not change the *logic*.\n")

    L.append("## 1. What this diagram shows\n")
    L.append(spec["prepare"] + "\n")

    L.append("## 2. Source code it references (verified)\n")
    L.append("| File | Reference |\n|---|---|")
    for path, desc in spec["refs"]:
        L.append(f"| `{path}` | {desc} |")
    L.append("")

    L.append("## 3. Nodes (layout spec)\n")
    L.append("| key | label | role/palette | shape | center (x, y) | size (w x h) |\n|---|---|---|---|---|---|")
    for n in spec["nodes"]:
        key, cx, cy, w, h, text, kind = n[0], n[1], n[2], n[3], n[4], n[5], n[6]
        shape = n[7] if len(n) > 7 else "rectangle"
        lbl = text.replace("\n", " / ")
        L.append(f"| `{key}` | {lbl} | {kind} | {shape} | ({cx}, {cy}) | {w} x {h} |")
    L.append("")

    L.append("## 4. Edges\n")
    L.append("| from | to | label | style |\n|---|---|---|---|")
    for e in spec["edges"]:
        a, b, label, dashed, two = e[0], e[2], e[4], e[5], e[6]
        style = ("dashed " if dashed else "solid ") + ("(two-way)" if two else "")
        L.append(f"| `{a}` | `{b}` | {label or '—'} | {style.strip()} |")
    L.append("")

    L.append("## 5. Palette (light professional — see 00-MASTER-GUIDE.md)\n")
    used = sorted({(n[6]) for n in spec["nodes"]})
    L.append("| role | fill | stroke |\n|---|---|---|")
    for k in used:
        f, s = PALETTE[k]
        L.append(f"| {k} | `{f}` | `{s}` |")
    L.append("")

    L.append("## 6. My version — importable Excalidraw scaffold\n")
    L.append("Hand-drawn style (`roughness: 1`, Virgil font), text positioned. "
             "Paste into a `.excalidraw` file or import via excalidraw.com "
             "(menu -> Open). Then refine spacing/routing — this is a starting point, "
             "not the final.\n")
    L.append("<details><summary>Excalidraw JSON</summary>\n")
    L.append("```json")
    L.append(json.dumps(scene_dict, indent=2))
    L.append("```")
    L.append("</details>\n")

    L.append("## 7. What to check before publishing\n")
    for c in spec["checklist"]:
        L.append(f"- [ ] {c}")
    L.append("- [ ] Hand-drawn look, light palette, no emojis, readable at blog/README width.")
    L.append("")
    return "\n".join(L)


MASTER = """# Diagram master guide — FinanceBench RAG Agent

This folder holds **specs** for the project's publication diagrams. Each `NN-*.md`
describes one diagram: what it shows, the verified code it is grounded in, a
node/edge layout table, an importable Excalidraw scaffold, and a check list.
Hand them to an Excalidraw-capable tool (e.g. Claude desktop with an Excalidraw
skill) to produce the final art. **Refine the visual layout; never change the
logic** — the flow is audited against the codebase.

## Visual style

- **Look:** Excalidraw hand-drawn (`roughness: 1`, Virgil/hand font, `fontFamily: 1`).
- **Background:** white (`#ffffff`). No dark mode.
- **Corners:** rounded rectangles (`roundness: {type: 3}`); diamonds for decisions.
- **Stroke width:** 2 for nodes, 2 for arrows. Dashed arrows for data-store / async / conditional links.
- **Arrowheads:** single-ended for flow; two-ended only for read/write store links.
- **Datastores:** rounded rect in the cyan `store` palette (cylinder shape optional if the tool supports it).
- **No emojis anywhere** (applies to README/PyPI and Medium alike).

## Light-professional palette (semantic, not decorative)

| Role | Use for | Fill | Stroke |
|---|---|---|---|
| client | CLI / API / user-facing | `#e7f5ff` | `#1971c2` |
| access | RBAC / research-agent / approver (access + agency) | `#ede0ff` | `#7048e8` |
| safety | guardrails / router / grader stages | `#fff3bf` | `#f08c00` |
| retr | retrieval / embedding / rerank | `#d0ebff` | `#1c7ed6` |
| gen | generator / final answer / success | `#d3f9d8` | `#2f9e44` |
| warn | hallucination check / retry / secondary judges | `#ffe8cc` | `#e8590c` |
| crit | HITL / blocked / decision diamonds / headline gate | `#ffe3e3` | `#e03131` |
| store | Qdrant / Postgres / Redis | `#c5f6fa` | `#0c8599` |
| none | pass-through / counts / scaffolding | `#f1f3f5` | `#868e96` |

Text ink: `#1e1e1e`. Note/caption ink: `#495057`.

## Conventions

- **Top-to-bottom** flow for pipelines; **left-to-right** for matrices/sequences.
- Decision points are diamonds; their out-edges are labeled with the condition.
- Keep node labels to <= 3 short lines. Put detail in the figure caption, not the box.
- One figure-caption sentence under each diagram, stating the takeaway (not the build journey).

## The diagrams

| # | File | Target | Shows |
|---|---|---|---|
| 1 | `01-architecture-hero.md` | README + PyPI | full 18-node request pipeline (hero) |
| 2 | `02-rbac.md` | Medium | role matrix + storage-layer enforcement |
| 3 | `03-hitl.md` | Medium | multi-party approval across sessions |
| 4 | `04-memory.md` | Medium | follow-up query rewriting from thread state |
| 5 | `05-retrieval-rerank-grade.md` | Medium | hybrid retrieval -> BGE rerank -> 3-stage grader |
| 6 | `06-research-agent.md` | Medium | selective decomposition subgraph |
| 7 | `07-guardrails.md` | Medium | PII + 3-layer injection cascade |
| 8 | `08-eval-methodology.md` | Medium + README | how the 72.7% is measured |

## Hard accuracy rules (carried from the project brief)

- **Reranker in production is STOCK `bge-reranker-v2-m3`** — no LoRA adapter active. Both FT variants were rolled back. Do not depict a fine-tuned reranker as live.
- **generator = Claude Sonnet 4.6**; hallucination check = Claude Sonnet 4.6 (settings.py:160,165 — both default + high-stakes, NOT Haiku); router/grader/entity = Llama-3.3-70B (Groq) with OpenAI fallback; research tier = gpt-4o-mini (decompose + sufficiency), Sonnet 4.6 (synthesize).
- **Eval numbers come from `docs/evaluation.md`** (72.7%, κ=0.932), never from raw `eval_results/*.json` — many are stale experiments (e.g. `*_calc.json` at 40.67%, `judge_eval_v1.json` at κ=0.8613).
- **No sprint numbers / trajectory language** on anything README-facing.
"""


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "00-MASTER-GUIDE.md").write_text(MASTER)
    print("wrote 00-MASTER-GUIDE.md")
    for name, spec in DIAGRAMS.items():
        scene = build(spec).to_dict()
        (OUT / f"{name}.md").write_text(md_for(name, spec, scene))
        # overlap sanity check
        boxes = [(e["x"], e["y"], e["width"], e["height"]) for e in scene["elements"]
                 if e["type"] in ("rectangle", "diamond")]
        ov = 0
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                ax, ay, aw, ah = boxes[i]
                bx, by, bw, bh = boxes[j]
                ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
                iy = max(0, min(ay + ah, by + bh) - max(ay, by))
                if ix * iy > 200:
                    ov += 1
        print(f"wrote {name}.md  (nodes={len(boxes)}, overlaps={ov})")
