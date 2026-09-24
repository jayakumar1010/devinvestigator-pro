# DevInvestigator

**When a GitHub Actions pipeline fails, DevInvestigator investigates it automatically and tells your
team what broke and how to fix it.** It reads the failed job's log, opens the relevant source files,
compares against the last successful run, and writes a root cause with a suggested fix.

It runs on one Linux server with Docker. No Kubernetes. The AI can be a **local model** (your GPU, so
nothing leaves your server) or an **API model** (no GPU needed).

A real result from the test repository:

> **Root cause:** `applyDiscount` in `lib/discount.js` subtracts the discount as a flat amount instead of a
> percentage. On line 6 it does `return price - percent;`, so `applyDiscount(200, 10)` returns 190 instead of 180.
> **Suggested fix:** change line 6 to `return price * (1 - percent / 100);`
> **Category:** test_failure  **Confidence:** 0.95  **Evidence:** 2 of 2 quotes verified

DevInvestigator **only suggests**. It never changes your code, never re-runs pipelines, and only ever
reads from GitHub.

---

## How it works

```
 Developer pushes code
          │
          ▼
 GitHub Actions pipeline runs  ──────────────►  passes ✓  (nothing happens)
          │
          │ fails ✗
          ▼
 GitHub sends a webhook  ──►  https://your-server/webhooks/github
                                        │
                  ┌─────────────────────▼──────────────────────────────┐
                  │  DevInvestigator (one Docker container)            │
                  │                                                    │
                  │  1. Check the signature, queue the investigation   │
                  │  2. Collect evidence from GitHub (read-only):      │
                  │        failed jobs · failed step · job log         │
                  │        commit · changed files                      │
                  │  3. Cut the error section out of the log           │
                  │  4. AI agent investigates, and may ask for more:   │
                  │        read a file · find the last successful run  │
                  │        compare the commits                         │
                  │  5. Check every quote the AI gives against the     │
                  │     real evidence, then cap the confidence         │
                  │  6. Save the result                                │
                  └─────────────────────┬──────────────────────────────┘
                                        │
                     ┌──────────────────┴──────────────────┐
                     ▼                                     ▼
        Web page /investigations                 CLI: app.cli list / show
        (root cause, fix, evidence)              (same data in JSON)
```

One investigation takes about 35–100 seconds with a local model, and they run one at a time.

---

## What you need

| | |
|---|---|
| A Linux server | with Docker and Docker Compose |
| A public HTTPS address | GitHub must be able to reach it (a domain, reverse proxy, or a tunnel) |
| A GitHub token | read-only, for the repositories you want investigated |
| An AI | **either** a GPU with about 20 GB free (local model) **or** an Anthropic / OpenAI API key |

---

## Step-by-step setup

### Step 1 — Get the code

```bash
git clone <your-repository-url> devinvestigator
cd devinvestigator
cp .env.example .env
```

Everything below is edits to `.env`. It holds your secrets and is never committed.

### Step 2 — Create the webhook secret

This is a password shared between GitHub and DevInvestigator, so nobody else can send it fake failures.

```bash
openssl rand -hex 32
```

Put it in `.env`:

```ini
GITHUB_WEBHOOK_SECRET=paste-the-value-here
```

### Step 3 — Create a GitHub token (read-only)

GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**

| Field | Value |
|---|---|
| Repository access | Only the repositories you want investigated |
| Permissions | **Actions: Read-only** and **Contents: Read-only** (Metadata is added automatically) |

*Contents: Read-only* is what lets the AI open source files such as `lib/discount.js`. Add it to `.env`:

```ini
GITHUB_TOKEN=github_pat_...
```

### Step 4 — Choose the AI

**Option A — you have a GPU (free, private: nothing leaves your server)**

If Ollama already runs on the host:

```ini
LLM_PROVIDER=ollama
OLLAMA_MODEL=hf.co/ggml-org/gemma-4-31b-it-GGUF:Q4_K_M
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

Or let DevInvestigator run its own Ollama in Docker on one GPU:

```bash
# .env:  OLLAMA_BASE_URL=http://ollama:11434   and   DEVINVESTIGATOR_GPU=0
docker compose --profile local-ai up -d
docker compose --profile local-ai exec ollama ollama pull hf.co/ggml-org/gemma-4-31b-it-GGUF:Q4_K_M
```

> ⚠️ Give the model a GPU with enough free memory. If it doesn't fit, it runs partly on the CPU,
> becomes many times slower, and investigations time out.

**Option B — no GPU (uses an API, costs money per investigation)**

```ini
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

or

```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
```

With an API provider, your logs and code snippets are sent to that provider. With `ollama` they never
leave your server. `vllm` + `OPENAI_BASE_URL` works too, for your own OpenAI-compatible server.

### Step 5 — Set a password for the web page

```bash
openssl rand -base64 24
```

```ini
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=paste-the-value-here
```

Without a password the page stays switched off.

### Step 6 — Start it

```bash
docker compose up -d --build
curl http://localhost:8012/health          # {"status":"healthy"}
docker compose logs -f devinvestigator     # Ctrl+C to stop watching
```

The startup log confirms your configuration:

```
GitHub integration: auth=token webhook_secret_configured=True
Investigations: database=sqlite auto_analyze=True mode=agent model=...
Web page /investigations: enabled (HTTP Basic auth)
```

### Step 7 — Make it reachable from the internet

GitHub has to reach your server over **HTTPS**. Put a reverse proxy (nginx, Caddy, Traefik) or a tunnel
in front of port `8012`. Check it from outside:

```bash
curl https://your-server/health
```

Only `/webhooks/github` has to be public. The web page can stay internal if you prefer.

### Step 8 — Add the webhook in GitHub

Repository → **Settings → Webhooks → Add webhook**

| Field | Value |
|---|---|
| Payload URL | `https://your-server/webhooks/github` |
| Content type | `application/json` |
| Secret | the value from Step 2 |
| SSL verification | Enable |
| Which events? | *Let me select individual events* → untick everything → tick **Workflow runs** only |
| Active | ✓ |

Click **Add webhook**. GitHub immediately sends a test "ping".

### Step 9 — Check it works

Open the webhook → **Recent Deliveries**. The `ping` should show **200** with `{"status": "pong"}`.

| Result | Meaning | Fix |
|---|---|---|
| ✅ 200 | Working | — |
| ❌ 401 | Secret mismatch | Use the same value in GitHub and `.env`, then `docker compose up -d` |
| ❌ 405 | Wrong URL | The path must end with `/webhooks/github` |
| ❌ 503 | No secret loaded | Set `GITHUB_WEBHOOK_SECRET`, then `docker compose up -d` |
| Nothing | GitHub can't reach you | Check DNS, HTTPS and firewall (Step 7) |

Now let a pipeline fail. After about a minute:

```bash
docker exec devinvestigator python -m app.cli list
```

```
  ID  STATUS     CREATED (UTC)     RUN                                 CATEGORY             CONF
   1  completed  2026-09-22 07:09  acme/frontend / Unit tests #1       test_failure         0.95
```

Then open **https://your-server/investigations** and log in.

---

## Where the results appear

**Web page** — `https://your-server/investigations`

- A list of failures with status, category and confidence.
- One page per investigation: root cause, suggested fix, confidence (and why it was capped), every
  quote with a ✓ or ✗ verification mark, the files the AI opened, the error section of the log, and
  the commit.
- Read-only, HTML-escaped, strict Content-Security-Policy, no caching, and only `https://` links.

**Command line**

```bash
docker exec devinvestigator python -m app.cli list                     # recent investigations
docker exec devinvestigator python -m app.cli show 1                   # one result as JSON
docker exec devinvestigator python -m app.cli show 1 --evidence        # include all collected evidence
docker exec devinvestigator python -m app.cli retry 1                  # investigate it again
docker exec devinvestigator python -m app.cli queue OWNER/REPO RUN_ID  # investigate an older failed run
```

`RUN_ID` is the number at the end of a run's URL: `.../actions/runs/RUN_ID`.

**Not built yet:** GitHub comments and Slack messages. Results live in the database and on the page.

---

## Telling developers (GitHub comments)

DevInvestigator can post the root cause and suggested fix as a comment on the failed **pull request**,
or on the **commit** when someone pushed straight to a branch.

```ini
NOTIFY_GITHUB_COMMENTS=true
GITHUB_COMMENT_TOKEN=github_pat_...        # a second token, used only for comments
PUBLIC_BASE_URL=https://your-server        # adds a link back to the full investigation
```

- **Off by default.** Commenting is the only write this tool can perform.
- **Separate token.** The investigation token stays read-only. The comment token needs
  *Pull requests: write*; add *Contents: write* only if you want comments on commits pushed directly
  to a branch. No other endpoint is ever called.
- **One comment per investigation.** The comment URL is stored, and a failed notification is recorded
  on the investigation without losing the result.

## Adding more repositories

Nothing changes inside the other repositories. No workflow edits.

1. **Give the token access:** add the repository to your fine-grained token (same read-only permissions).
2. **Add a webhook,** exactly as in Step 8.

For many repositories in one organization, add **one organization webhook**
(Organization → Settings → Webhooks) instead of one per repository. It covers every repository,
including new ones.

For many organizations, a **GitHub App** is cleaner (`GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY_PATH`).
The code supports it, but it has only been tested with unit tests, not against real GitHub.

---

## What the AI is allowed to do

```
AI asks for evidence ──► only these read-only tools:
                          · get_file                    (a file at the failing commit)
                          · get_previous_successful_run (the last run that passed)
                          · compare_commits             (what changed since then)
                         ▼
AI answers ──► checked before you ever see it:
                · every quote must appear word-for-word in the evidence sent
                · the answer must match a fixed schema, or it is rejected
                · confidence is capped in code: never 1.0; at most 0.89 when the
                  cause is only inferred or a quote failed verification
```

- **Read-only, always.** Every GitHub call is a GET. DevInvestigator cannot push code, re-run
  workflows, comment, or change anything.
- **Scoped to the failure.** The AI chooses only a tool and a file path; the repository, commit and
  branch come from the failed run, so it cannot reach other repositories.
- **Log text is untrusted.** Instructions hidden in a log or source file are ignored.
- **Secrets never leak into output.** Tokens are masked, and log-download URLs are kept out of logs
  and error messages.

---

## Configuration reference

Everything is set in `.env`. Full list with comments: `.env.example`.

**GitHub**

| Variable | Default | Purpose |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | — | **Required.** Without it every delivery is rejected with 503 |
| `GITHUB_TOKEN` | — | Read-only fine-grained token |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` | — | GitHub App instead of a token |
| `GITHUB_API_URL` | `https://api.github.com` | Change for GitHub Enterprise Server |
| `GITHUB_LOG_MAX_BYTES` | `200000` | How much of each job log is kept |
| `GITHUB_MAX_FAILED_JOB_LOGS` | `5` | Logs fetched when many jobs fail |

**AI**

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama`, `anthropic`, `openai` or `vllm` |
| `LLM_MODEL` | — | Overrides the provider's model below |
| `OLLAMA_MODEL`, `OLLAMA_BASE_URL` | gemma-4-31b, host | Local model |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | —, `claude-opus-5` | Anthropic |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | —, `gpt-4o`, — | OpenAI or any compatible server |
| `LLM_TIMEOUT_SECONDS` | `300` | Limit for one AI call |
| `LLM_MAX_RETRIES` | `2` | Retries for connection errors and rate limits |
| `OLLAMA_NUM_CTX` | `16384` | Context window; oversized prompts are refused, not truncated |
| `DEVINVESTIGATOR_GPU` | `0` | GPU for the bundled Ollama container |

**Investigations**

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite on a volume | `postgresql+asyncpg://...` for PostgreSQL |
| `AUTO_ANALYZE` | `true` | `false` = collect evidence, skip the AI |
| `ANALYSIS_MODE` | `agent` | `single_pass` = one AI call, no tools |
| `INVESTIGATION_TIMEOUT_SECONDS` | `900` | Limit for one whole investigation |
| `AGENT_MAX_TOOL_CALLS` | `6` | How much evidence the AI may request |

**Web page and server**

| Variable | Default | Purpose |
|---|---|---|
| `DASHBOARD_PASSWORD` | — | **Required to enable the page** |
| `DASHBOARD_USERNAME` | `admin` | Login name |
| `ENABLE_API_DOCS` | `false` | `/docs` and `/openapi.json` |
| `DEVINVESTIGATOR_PORT` | `8012` | Host port |
| `DEVINVESTIGATOR_SUBNET` | `10.211.11.0/24` | Docker network subnet |

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| Webhook shows **401** | Secret differs | Same value in GitHub and `.env`, then `docker compose up -d` |
| Webhook shows **503** | Secret not loaded | `.env` is only read at startup: `docker compose up -d` |
| Webhook shows **405** | URL missing the path | Use `.../webhooks/github` |
| Investigation `failed`, "ReadTimeout" | Model too slow, usually a busy GPU | Free the GPU, use a smaller model, or switch to an API provider |
| Investigation `failed`, "404" | Token can't read that repo | Add the repository to the token |
| Evidence has errors but the run finished | One source failed (e.g. expired logs) | Normal; the rest is still collected |
| Web page returns **503** | No password set | Set `DASHBOARD_PASSWORD`, then `docker compose up -d` |
| Results look stale | The page auto-refreshes only while work is queued | Reload |
| Nothing arrives at all | GitHub can't reach the server | Test `curl https://your-server/health` from outside |

Useful commands:

```bash
docker compose logs -f devinvestigator          # live logs
docker compose ps                               # status and health
docker exec devinvestigator python -m app.cli list
```

---

## Inside the project

```
app/
├── api/            webhook and health endpoints
├── core/           settings, logging, webhook signature check
├── integrations/
│   └── github/     read-only GitHub client, auth, models, failure rules
├── evidence/       collect evidence, cut the error section out of logs
├── agent/          agent loop and its three read-only tools
├── analysis/       prompt, output schema, quote checking, confidence policy
├── llm/            provider interface + ollama / anthropic / openai
├── database/       models, session, queries (the queue lives here)
├── web/            the web page (templates, views, auth)
├── orchestrator.py the worker: queue → evidence → AI → stored result
└── cli.py          list, show, queue, retry, preview, analyze, investigate
```

The AI layer is swappable: `app/analysis` and `app/agent` only use the `LLMProvider` interface in
`app/llm/base.py`, so adding a provider never changes the investigation logic.

**Development**

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/pytest -q                                   # 232 tests, no network calls
.venv/bin/uvicorn app.main:app --reload --port 8012   # run outside Docker
```

**Docker facts**

- One container (plus an optional Ollama one), non-root user, healthcheck on `/health`.
- Own network `devinvestigator_net`; host port `8012` → container `8000`.
- Database on the volume `devinvestigator_data`, so results survive rebuilds.
- Interrupted investigations are picked up again when the container restarts.

---

## Status

**Working:** GitHub Actions webhook · read-only evidence collection · error extraction from logs ·
local or API AI · agent with three tools · quote verification · confidence limits · automatic
investigation of every failure · stored history · web page · CLI.

**Not built yet:** GitHub comments and Slack notifications · GitLab and Jenkins · database migrations
(schema changes need care on upgrade) · data retention (evidence grows forever) · repository allowlist ·
PostgreSQL is supported but not yet tested in production.

**Known limits:** one investigation at a time (35–100 s each with a local model); a shared GPU can
slow investigations enough to time out; GitHub App authentication is untested against real GitHub.
