# Upgrade Playbook

Cookbook for "the maintainer pushed a change, how do I get it?" — one entry per common scenario. Most paths reduce to `financebench upgrade`; a few require an extra step (corpus re-ingest, CLI republish, breaking-change negotiation).

The premise: **the project is architected so the same user command works for ~90% of realistic future changes**. The architectural seams that make this hold are documented in [DEPLOYMENT_PLAN.md Section 18](../DEPLOYMENT_PLAN.md) (the deployment plan, internal). This page is the cookbook view: change type → exact commands.

## The default workflow

```bash
financebench upgrade
```

What that runs:

1. `git status` in `~/.financebench/repo` — refuses if working tree is dirty (override with `--force`)
2. `git pull --ff-only`
3. `docker compose pull` (refreshes pinned images: qdrant, postgres, redis-stack)
4. `docker compose build api` (incorporates the new source code into the api image)
5. `docker compose up -d --force-recreate api` (restarts the api with the new image; preserves volumes)
6. Polls `/v1/health` until 200 (or 6 min timeout)

Data preservation: all named volumes survive (`pg_data`, `qdrant_data`, `redis_data`, `hf_cache`) plus the host-mounted `cost_logs/` and `logs/`. Chat history, ingested corpora, cost trail all intact.

## Recipes by change type

### "I tweaked a prompt template"

```bash
financebench upgrade
```

That's it. The new prompt ships with the rebuilt api image. Zero data migration.

### "I changed the generator model (e.g. claude-sonnet-4-6 → claude-sonnet-5)"

If the change is in `src/services/llm_factory.py`:

```bash
financebench upgrade
```

If you want to override without rebuilding (faster):

```bash
# Edit .env in your cloned repo:
GENERATOR_MODEL=claude-sonnet-5
# Then:
docker compose -f compose.minimal.yml restart api
```

The api will pick up the env change on restart. Verify with `financebench status` → "Backend semver" + the Sprint 7.19 boot banner (`docker compose logs api | grep "generator:"`).

### "I added a new LangGraph node"

```bash
financebench upgrade
```

Node graph changes ship with the api image rebuild. The CLI is unaffected — it sees the same SSE events. If the new node should surface progress in the CLI, the maintainer also added it to `_NODE_LABELS` in `src/api/routes/chat.py` so the spinner label updates.

### "I swapped the embedding model" (DESTRUCTIVE — re-ingest required)

Cross-embedding queries return garbage (incompatible vector spaces). The boot banner's fingerprint check warns when the live `EMBEDDING_PROVIDER` doesn't match what's stored in the Qdrant collection sentinel:

```
WARNING: collection 'financial_docs' was created with voyage-finance-2 (1024-dim)
WARNING: current EMBEDDING_PROVIDER is openai (text-embedding-3-small, 1536-dim)
WARNING: queries will return garbage or fail with VectorDimensionError.
WARNING: re-ingest the corpus with the new embedding to fix.
```

Fix:

```bash
financebench upgrade

# Drop the stale collection and re-seed
docker compose -f compose.minimal.yml exec api python -c "
from qdrant_client import QdrantClient
from src.config.settings import settings
QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT).delete_collection(settings.QDRANT_COLLECTION)
"
docker compose -f compose.minimal.yml exec api python scripts/seed_qdrant.py --sample
```

Time: ~60s for the 8-PDF sample, ~3.5h + ~$0.82 for the FinanceBench full corpus.

### "I added a new endpoint (additive, backwards-compatible)"

```bash
financebench upgrade
```

Old CLI keeps working — it ignores endpoints it doesn't call. To use the new endpoint from the CLI, the maintainer also added a slash command or subcommand and republished the CLI:

```bash
pip install --upgrade financebench-rag-agent
```

### "I added a new RBAC role"

```bash
financebench upgrade
```

Per [src/services/roles_service.py](../src/services/roles_service.py), roles are DB-first with a fallback to `ROLE_PERMISSIONS` in `src/config/rbac_config.py`. New roles can be added via:

- Editing `ROLE_PERMISSIONS` and rebuilding (ships with the api image)
- OR using `POST /v1/admin/roles` while the api is running (dynamic, no rebuild)

Approval hierarchy (`CAN_APPROVE_FOR`) lives in source code — to grant a new role approval authority, edit `src/config/rbac_config.py` and `financebench upgrade`.

### "I'm doing a breaking API change"

(Rare. The plan is to keep `/v1/*` stable.)

The right pattern:

1. Add new endpoints under `/v2/` prefix while keeping `/v1/` working.
2. Update the CLI to support BOTH versions; bump CLI semver.
3. The CLI's `Accept` header (`application/vnd.financebench.v1+json`) gives the backend a chance to disambiguate.
4. Eventually deprecate v1 over one or two minor versions.
5. Users on the old CLI get a clear error message pointing to `pip install --upgrade financebench-rag-agent`.

For the user, the upgrade command stays the same:

```bash
financebench upgrade
pip install --upgrade financebench-rag-agent
```

### "I'm upgrading past 0.1.3 on a machine that previously ran 0.1.3 (M1 / Apple Silicon)"

0.1.3 mounted the HuggingFace cache volume at the wrong path inside the container (`/root/.cache/huggingface` while the api runs as `appuser`). 0.1.4 fixes the mount to `/home/appuser/.cache/huggingface` AND pre-creates the directory with `appuser` ownership so Docker copies the right permissions into a fresh volume on first mount.

But the volume `repo_hf_cache` already exists from your 0.1.3 run with root-owned permissions. Docker reuses existing volumes as-is — the Dockerfile fix doesn't help if the volume is already in the wrong state. You must wipe it once:

```bash
cd ~/.financebench/repo
git pull
docker compose -f compose.minimal.yml down -v   # -v removes named volumes
docker compose -f compose.minimal.yml up -d --build
```

`down -v` also wipes Qdrant (your seeded corpus), Postgres (HITL state), and Redis (result cache). The wizard's `_seed_sample_corpus` step re-seeds the sample 8 PDFs automatically on the next `financebench setup` (~60s, ~$0.0005 in embeddings).

Verify the fix landed by inspecting the volume permissions after the rebuild:

```bash
docker exec repo-api-1 stat -c '%U:%G %a' /home/appuser/.cache/huggingface
# Should print: appuser:appuser 755
```

If it still prints `root:root`, the wipe didn't happen — re-run `down -v` and `up -d --build`.

### "I want to roll back an upgrade"

The upgrade command doesn't snapshot — rollback is manual:

```bash
cd ~/.financebench/repo
git log --oneline -10               # find the prior commit
git checkout <prior-commit-sha>
docker compose -f compose.minimal.yml build api
docker compose -f compose.minimal.yml up -d --force-recreate api
```

Data volumes are unaffected (rollback only changes code, not data).

## What `upgrade` does NOT do

- **Doesn't migrate Postgres schemas other than what alembic runs at api startup.** If a release adds a new alembic revision, it runs automatically on the next api boot. If a release REMOVES a column you care about, that's a destructive change — you must back up before upgrading.
- **Doesn't touch your `.env`.** New required env vars in a release won't be auto-set. The boot will fail with a clear error (`startup model_validator`) and you edit `.env` then restart.
- **Doesn't auto-rotate secrets.** `JWT_SECRET`, Langfuse defaults — your responsibility per [docs/deploy.md](deploy.md) production hardening checklist.
- **Doesn't push to GitHub.** It pulls from the upstream repo. If you've forked or made local commits, `financebench upgrade --force` is required to bypass the dirty-tree check.

## Diagnostics if upgrade fails

```bash
# 1. Container logs
docker compose -f compose.minimal.yml logs api | tail -50

# 2. Boot banner — what actually loaded?
docker compose -f compose.minimal.yml logs api | grep -A 15 "Pipeline boot"

# 3. Health probe directly
curl -v http://localhost:8000/v1/health

# 4. Embedding fingerprint mismatch?
docker compose -f compose.minimal.yml logs api | grep -i fingerprint

# 5. Cost log still being written?
ls -la cost_logs/cost_log.jsonl

# 6. Full restart from scratch
financebench down
financebench setup     # NOT setup --full unless you started full
```

## Maintainer-side: what changes require a CLI re-publish

| Maintainer change | Requires CLI republish? |
|---|---|
| Prompt template tweak | No |
| Reranker model swap | No |
| Generator model swap | No |
| New backend endpoint (additive) | Only if CLI should expose it |
| New SSE event type | No (CLI ignores unknown types per Section 18.3.2) |
| New CLI slash command | Yes |
| Different rendering / colour | Yes |
| API breaking change (rare) | Yes, in lockstep |

The CLI versions independently from the backend on PyPI. Backend changes ship via container image rebuild + git pull. CLI changes ship via `pip install --upgrade`.
