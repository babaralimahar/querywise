# Deployment

Target: **Render free web service** (Docker) + **Neon free Postgres**, the same as P1-P3. No paid
services.

> Status: **not deployed yet.** All five projects get deployed together at the end.

## Steps

1. **Push to GitHub.** From `D:\AI Projects\querywise`, add a remote and push with tags.
2. **Neon.** Create a project; copy the **direct** connection string (owner role).
3. **Migrate and load from your machine** (owner role; creates `analyst_ro`):
   ```powershell
   $env:DATABASE_URL="<neon owner url>"; $env:ANALYST_RO_PASSWORD="<random 16+ chars, [A-Za-z0-9_-]>"
   uv run alembic upgrade head
   uv run python -m querywise.data.load
   ```
   The synthetic warehouse is ~40k rows: well inside Neon's free tier. If Neon refuses
   `CREATE ROLE` from SQL, create `analyst_ro` in the Neon console and re-run the migration
   (it only alters an existing role).
4. **Render.** New -> Web Service -> Docker. Environment:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Neon owner URL (app tables: audit, cache, feedback, API keys) |
   | `ANALYTICS_DATABASE_URL` | Same host, user `analyst_ro` + its password |
   | `ENV` | `prod` |
   | `LLM_PROVIDER` | `openai_compat` |
   | `LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` |
   | `LLM_MODEL` | `gemini-3.1-flash-lite` |
   | `LLM_API_KEY` | your Gemini key (free tier) |
   | `EMBED_PROVIDER` | `openai_compat` |
   | `WORKER_MODE` | `off` |

5. **Keys.** `uv run python -m app.cli create-key --name analyst1 --role viewer` (analysts),
   `--role admin` for the audit endpoint.
6. **Verify.** `/readyz` 200; ask "Top 5 categories by revenue in 2018" at `/ui`; ask "List
   customer zip codes" and see it blocked or refused.

## Production notes

- **Free-tier quota:** Gemini free tier is 500 requests/day; one question costs 2 requests
  (SQL + summary). Set `SUMMARY_MODE=template` to halve that.
- **Provider retention:** questions and result digests go to the model provider. Use a
  no-training tier before real data, and keep digests to aggregates.
- **Row-level security** per analyst is the next step: Postgres RLS on the views keyed by
  `SET app.analyst_id` in the executor's transaction.
