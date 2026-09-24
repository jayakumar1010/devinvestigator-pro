<div align="center">

# 🔎 DevInvestigator

### Your pipeline failed. Know why in two minutes, not two hours.

*An AI agent that investigates failed CI/CD pipelines, proves its answer against the real logs,
and tells your team how to fix it.*

![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-supported-2088FF?logo=githubactions&logoColor=white)
![AI](https://img.shields.io/badge/AI-local%20or%20API-7C3AED)
![Tests](https://img.shields.io/badge/tests-295%20passing-2ea44f)
![Read only](https://img.shields.io/badge/GitHub%20access-read--only-6b7280)

[Why it exists](#why-it-exists) · [What you get](#what-you-get) · [Who it is for](#who-it-is-for) ·
[How it works](#how-it-works) · [Setup](#setup-step-by-step) · [Status](#status--roadmap)

</div>

---

When a GitHub Actions run fails, DevInvestigator investigates it by itself: it reads the failing job's
log, opens the code at the broken commit, compares against the last run that passed, then posts the
root cause and the fix as a comment on your commit or pull request.

It runs on **one Linux server with Docker**. No Kubernetes. The AI can be a **local model** on your GPU
(nothing leaves your server) or an **API model** (no GPU needed).

> **Real result from the test repository**
>
> **Root cause:** There is a version mismatch between `react` and `react-dom` in `web/package.json`. The project
> specifies react@17.0.2 and react-dom@18.3.1, but react-dom@18.3.1 requires a peer dependency of react@^18.3.1.
> **Suggested fix:** Update `react` to 18.3.1 to match what react-dom requires.
> **Category:** dependency_failure · **Confidence:** 0.95 (direct) · **Evidence:** 4 of 4 quotes verified · **Time:** 109s

DevInvestigator **only suggests**. It never changes code, never re-runs pipelines, and reads from
GitHub through GET requests only. The single exception is posting a comment, which is off by default
and uses its own separate token.

---

## Why it exists

A pipeline fails at 11 p.m. Somebody opens GitHub, scrolls 4,000 lines of log, finds the one line that
matters, opens the file, guesses which recent change broke it, and writes it up for the team. Twenty
minutes, for a failure the team has probably seen before.

```
   WITHOUT DevInvestigator                    WITH DevInvestigator
   ───────────────────────                    ────────────────────
   pipeline fails                             pipeline fails
        │                                          │
        ▼                                          ▼  (automatic, nobody waits)
   someone notices … eventually            evidence collected in ~1 s
        │                                          │
        ▼                                          ▼
   open Actions, find the job              error section extracted
        │                                          │
        ▼                                          ▼
   scroll thousands of log lines           AI reads the code at that commit
        │                                          │
        ▼                                          ▼
   open the code, guess the cause          every quote verified, confidence capped
        │                                          │
        ▼                                          ▼
   write it up for the team                comment on the commit or PR
   ~20 minutes, one person                 ~2 minutes, nobody
```

## What you get

| | Payan |
|---|---|
| ⏱️ **Time back** | The investigation happens while nobody is watching. The developer opens GitHub and the answer is already there. |
| 🌙 **Failures never sit unexplained** | Night, weekend, on leave: the comment is written anyway. |
| 🔁 **The same problem is never investigated twice** | A repeat is answered in ~1 second, free, with the same words as last time. |
| 🐛 **Flaky tests stop hiding** | *"This failure has happened 7 times recently"* is often more useful than any single root cause. |
| 🧾 **Evidence you can check** | Every quoted line is verified against the real log. No invented errors. |
| 🎓 **Juniors are unblocked** | The fix arrives with its evidence, instead of waiting for a senior to have a free moment. |
| 🔒 **Your code stays yours** | With a local model, no log and no source line ever leaves your server. |
| 📈 **It improves with use** | Rules grow, repeats get cheaper, and the quality page shows whether it is actually helping. |

### It gets better the longer you run it

```
   week 1          every failure → AI investigation            ~110 s each
       │
       ▼           repeats start matching
   week 2–3        known failures → reused answer              ~1 s, free
       │
       ▼           team writes .devinvestigator.yml rules
   month 2         "we know this one" → instant rule answer    ~1 s, free, always identical
       │
       ▼           feedback buttons collect correct / wrong
   ongoing         quality page shows if it earns its place
```

## Who it is for

| Situation | Worth it? | Why |
|---|---|---|
| **A team with several repositories** | ✅ Strongly | Nobody has to be the person who reads CI logs |
| **Pipelines that fail often** (flaky tests, shared runners) | ✅ Strongly | Repeat detection turns noise into a countable fact |
| **Juniors or new joiners in the team** | ✅ Yes | The fix arrives with evidence attached |
| **Private code that cannot leave the company** | ✅ Yes | Local model on your own GPU |
| **Long or matrix builds** (thousands of log lines) | ✅ Yes | The error section is extracted automatically |
| **Open-source maintainers** | ✅ Yes | Contributors get the cause on their PR without you triaging |
| **One developer, one repository, rare failures** | ⚠️ Probably not | Reading the log yourself takes 30 seconds |
| **Failures that need a human decision** (design, product) | ❌ No | It explains what broke, not what you should want |

### When you do not need it

Be honest with yourself: if your pipeline fails twice a month with an obvious error, open the log. This
tool earns its place when failures are **frequent**, **noisy**, or land on **someone who did not write
the code** — that is where twenty minutes disappear, over and over.

## Contents

| | |
|---|---|
| [How it works](#how-it-works) | The whole flow, and how an answer is produced |
| [What makes it different](#what-makes-it-different) | Verified evidence, honest confidence, read-only |
| [Setup](#setup-step-by-step) | Nine steps from clone to first comment |
| [Where results appear](#where-the-results-appear) | Comment, web page, quality page, CLI |
| [Features](#features) | What exists today, with measured numbers |
| [Configuration](#configuration) | Every setting, grouped |
| [Safety](#safety) | What the AI may and may not do |
| [Troubleshooting](#troubleshooting) | Real problems and their fixes |
| [Status & roadmap](#status--roadmap) | Built, not built, known limits |

---

## How it works

```
   Developer pushes code
            │
            ▼
   GitHub Actions runs ───────────────► passes ✓  nothing happens
            │
            │ fails ✗
            ▼
   GitHub webhook  ──►  POST /webhooks/github        (signature checked, 202 in milliseconds)
            │
            ▼
   ┌──────────────────── devinvestigator container ────────────────────┐
   │                                                                   │
   │   queue (the database)  ──►  worker, one investigation at a time  │
   │                                      │                            │
   │                                      ▼                            │
   │   1. Collect evidence (read-only GitHub API)                      │
   │        failed jobs · failed step · job log · commit · files       │
   │                                      │                            │
   │   2. Cut the error section out of the log (134 lines → 13)        │
   │                                      │                            │
   │   3. Answer it — cheapest route first:                            │
   │        ├─ team rule matches?      → instant, no AI                │
   │        ├─ same failure as before? → reuse, no AI                  │
   │        └─ otherwise               → AI agent investigates         │
   │                                      │                            │
   │   4. Check the answer                                             │
   │        every quote verified · schema enforced · confidence capped │
   │                                      │                            │
   │   5. Store it, then notify                                        │
   └──────────────────────────────────────┬────────────────────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              ▼                           ▼                           ▼
     GitHub comment              Web page + quality           CLI (list / show)
   on the commit or PR          /investigations                app.cli
```

### How an answer is produced

```
                      ┌─────────────────────────┐
   failure evidence ──►   Team rule matches?     │──yes──► answer from the rule    ~1 s   free
                      └───────────┬─────────────┘
                                  │ no
                      ┌───────────▼─────────────┐
                      │  Same failure as before? │──yes──► reuse previous answer   ~1 s   free
                      └───────────┬─────────────┘
                                  │ no
                      ┌───────────▼─────────────┐
                      │      AI agent            │
                      │  may ask for more:       │
                      │   · get_file             │
                      │   · search_repository    │──────► fresh answer        35–110 s
                      │   · get_workflow_file    │
                      │   · previous successful  │
                      │   · compare_commits      │
                      └──────────────────────────┘
```

### Measured on the test repository

| Run | Route | Time | AI calls | Result |
|---|---|---|---|---|
| CI #1 | AI agent (3 tool calls) | **109 s** | 5 | Correct root cause, 4/4 quotes verified |
| CI #2 | Reused (same failure) | **1.27 s** | 0 | Same answer, comment posted |
| CI #3 | Reused (same failure) | **1.62 s** | 0 | Same answer, comment posted |

---

## What makes it different

Most "AI reads your logs" tools stop at the answer. The work here is in **not trusting the answer**:

| | How it works | Why it matters |
|---|---|---|
| **Every quote is verified** | Each line the AI cites is checked word-for-word against the log or file it was actually given | An AI that invents a log line is worse than useless; invented quotes are flagged, not shown |
| **Confidence enforced in code** | Never 1.0. At most 0.89 when the cause is inferred or any quote failed verification. 0.5 when the evidence is insufficient | The model cannot talk its way past the limits |
| **Read-only by design** | Every GitHub call is a GET; a test asserts it | The tool cannot break your repository |
| **Local AI option** | Ollama on your GPU | Your logs and code never leave your server |
| **Untrusted log text** | Log and file content is fenced and marked untrusted | Instructions hidden in a log cannot steer the investigation |
| **Cheapest route first** | Team rule → reuse → AI | Known and repeated failures cost nothing |

---

## Setup, step by step

### Requirements

| | |
|---|---|
| Linux server | Docker + Docker Compose |
| Public HTTPS address | GitHub must reach it (domain, reverse proxy, or tunnel) |
| GitHub token | read-only, for the repositories you want investigated |
| AI | a GPU with ~20 GB free **or** an Anthropic / OpenAI API key |

### 1. Get the code

```bash
git clone git@github.com:jayakumar1010/devinvestigator-pro.git devinvestigator
cd devinvestigator
cp .env.example .env
```

### 2. Webhook secret

```bash
openssl rand -hex 32
```
```ini
GITHUB_WEBHOOK_SECRET=<paste>
```

### 3. Read-only GitHub token

GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens**

| Field | Value |
|---|---|
| Repository access | Only the repositories to investigate |
| Permissions | **Actions: Read-only**, **Contents: Read-only** |

```ini
GITHUB_TOKEN=github_pat_...
```

*Contents: Read-only* is what lets the agent open source files.

### 4. Choose the AI

**With a GPU** — free and private:
```ini
LLM_PROVIDER=ollama
OLLAMA_MODEL=hf.co/ggml-org/gemma-4-31b-it-GGUF:Q4_K_M
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

Or let DevInvestigator run its own Ollama on one GPU:
```bash
# .env: OLLAMA_BASE_URL=http://ollama:11434  and  DEVINVESTIGATOR_GPU=0
docker compose --profile local-ai up -d
docker compose --profile local-ai exec ollama ollama pull hf.co/ggml-org/gemma-4-31b-it-GGUF:Q4_K_M
```

> ⚠️ The model must fit in GPU memory. If it spills to the CPU it becomes many times slower and
> investigations time out.

**Without a GPU** — costs money per investigation:
```ini
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```
or `LLM_PROVIDER=openai` with `OPENAI_API_KEY`, or `vllm` with `OPENAI_BASE_URL`.

### 5. Web page password

```bash
openssl rand -base64 24
```
```ini
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=<paste>
```
Without a password the page stays switched off.

### 6. Start

```bash
docker compose up -d --build
curl http://localhost:8012/health          # {"status":"healthy"}
docker compose logs -f devinvestigator
```

The startup log confirms the configuration:
```
GitHub integration: auth=token webhook_secret_configured=True
Investigations: database=sqlite auto_analyze=True mode=agent model=...
Web page /investigations: enabled (HTTP Basic auth)
```

### 7. Make it reachable

GitHub must reach the server over **HTTPS**. Put nginx, Caddy, Traefik or a tunnel in front of port
`8012`. Only `/webhooks/github` has to be public.

```bash
curl https://your-server/health
```

### 8. Add the webhook

Repository (or organization) → **Settings → Webhooks → Add webhook**

| Field | Value |
|---|---|
| Payload URL | `https://your-server/webhooks/github` |
| Content type | `application/json` |
| Secret | the value from step 2 |
| Which events? | *Let me select individual events* → **Workflow runs** only |

### 9. Check

Webhook → **Recent Deliveries** → the ping should be **200** `{"status":"pong"}`.

| Result | Meaning | Fix |
|---|---|---|
| ✅ 200 | Working | — |
| ❌ 401 | Secret mismatch | Same value in GitHub and `.env`, then `docker compose up -d` |
| ❌ 405 | Wrong URL | The path must end with `/webhooks/github` |
| ❌ 503 | No secret loaded | `.env` is read at startup only |
| Nothing | GitHub cannot reach you | Check DNS, HTTPS, firewall |

Then let a pipeline fail and watch:
```bash
docker exec devinvestigator python -m app.cli list
```

### 10. Optional: comment on GitHub

```ini
NOTIFY_GITHUB_COMMENTS=true
GITHUB_COMMENT_TOKEN=github_pat_...    # a second token, comments only
PUBLIC_BASE_URL=https://your-server
```

The comment token needs **Pull requests: write**, plus **Contents: write** only if you also want
comments on commits pushed straight to a branch.

---

## Where the results appear

### 1. GitHub comment

```markdown
## 🔎 DevInvestigator: `CI` #1 failed
**Failed stage:** `install / Install dependencies`

### Root cause
There is a version mismatch between 'react' and 'react-dom' in web/package.json …

### Suggested fix
Update the version of 'react' in web/package.json to 18.3.1 …

**Category:** `dependency_failure` · **Confidence:** 0.95 (direct) · **Evidence:** 4/4 verified

<details><summary>Evidence (checked against the real logs and files)</summary>
  job log · npm error code ERESOLVE
  web/package.json line 6-7 · "react": "17.0.2", "react-dom": "18.3.1"
</details>
```

### 2. Web page — `/investigations`

| Page | Shows |
|---|---|
| List | Every investigation: status, repository, workflow, category, confidence, duration |
| Detail | Root cause, fix, confidence with the reason for any cap, evidence with ✓/✗, files the agent opened, the error section of the log, the commit, a repeat warning |
| **Quality** `/investigations/stats` | Totals, average confidence, verified-evidence ratio, answers reused and time saved, correct/wrong feedback tally, breakdowns by status, category and model |

Each investigation has a **"Was this answer right?"** button, so quality is measured, not assumed.

### 3. Command line

| Command | What it does |
|---|---|
| `app.cli list` | Recent investigations |
| `app.cli show ID` | One result as JSON (`--evidence` for everything collected) |
| `app.cli comment-preview ID` | The exact GitHub comment, without posting |
| `app.cli queue OWNER/REPO RUN_ID` | Investigate an older failed run |
| `app.cli retry ID` | Investigate again |
| `app.cli preview OWNER/REPO RUN_ID` | Exactly what would be sent to the AI, no AI call |
| `app.cli analyze OWNER/REPO RUN_ID` | One-shot analysis, no tools |
| `app.cli investigate OWNER/REPO RUN_ID` | Agent investigation (`--show-transcript` for every message) |

All are run as `docker exec devinvestigator python -m app.cli …`

---

## Features

### Evidence collected (all read-only)

| Source | Collected | Limits |
|---|---|---|
| Workflow run | number, attempt, event, branch, commit, timestamps | — |
| Jobs | jobs and steps of that attempt | 100/page, 10 pages |
| Job logs | the failing section, cut out with the `##[error]` marker | 200 KB tail → 120 lines / 8000 chars |
| Commit | message, author, changed files | first 50 files in the prompt |
| Files *(agent)* | any file or directory at the failing commit | 12 000 chars |
| Code search *(agent)* | files matching a term | 10 results |
| Workflow file *(agent)* | the failing workflow definition | — |
| Previous successful run *(agent)* | last passing run of the same workflow and branch | 20 runs checked |
| Commit comparison *(agent)* | commits, files and diffs since that run | 30 commits, 15 000 chars |

If one source fails (expired logs, a 404), it is recorded in `errors` and the rest is still collected.

### Answer routes

| Mode | When | Cost | Time |
|---|---|---|---|
| `rule` | A team rule in `.devinvestigator.yml` matches | free | ~1 s |
| `reused` | The same failure was investigated before | free | ~1 s |
| `agent` | Default: the AI may request more evidence | 4–6 AI calls | 35–110 s |
| `single_pass` | `ANALYSIS_MODE=single_pass`: one call, no tools | 1 AI call | 15–60 s |

### Repeated failures

Each failure gets a fingerprint from repository + workflow + failed step + the first error lines, with
numbers, versions, commit hashes and paths normalised away. A match reuses the previous answer and the
comment says *"This failure has happened 4 times recently"* — which is how a flaky test shows itself.

### Team rules

```yaml
# .devinvestigator.yml in the repository being investigated
rules:
  - name: Test database was not ready
    match: "ECONNREFUSED"
    category: infrastructure_failure
    root_cause: The job could not reach the test database container.
    fix: Re-run the job; if it repeats, add a health check to the service in the workflow.
    confidence: 0.85
```

The matched log line becomes the evidence, so it passes the same verification. Rule confidence is capped
at 0.9. Broken YAML or a broken rule is skipped, never fatal. Three ready-made rules are in
`examples/devinvestigator.yml`.

### AI providers

| `LLM_PROVIDER` | GPU | Evidence leaves your server | Settings |
|---|---|---|---|
| `ollama` (default) | Yes | No | `OLLAMA_MODEL`, `OLLAMA_BASE_URL` |
| `anthropic` | No | Yes, to Anthropic | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` |
| `openai` | No | Yes, to OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL` |
| `vllm` | Your own server | No | `OPENAI_BASE_URL`, `OPENAI_MODEL` |

The investigation code only uses the `LLMProvider` interface in `app/llm/base.py`, so a provider can be
swapped without touching any investigation logic.

---

## Safety

```
The AI may ask for                     The AI can never
──────────────────────                 ──────────────────────────────
· get_file                             · push code
· search_repository                    · re-run or cancel a workflow
· get_workflow_file                    · change a repository setting
· get_previous_successful_run          · reach another repository
· compare_commits                      · act on instructions hidden in a log
  (all GET requests)
```

| Protection | How |
|---|---|
| Webhook authenticity | HMAC-SHA256 over the raw body, constant-time compare; no secret → 503 |
| Read-only investigation | Every call is a GET; a test enforces it |
| Comment writing | Separate token, off by default, two comment endpoints only |
| Scope | The model picks only a tool and a path; repository, commit and branch come from the failed run |
| Path safety | `..` and backslashes refused |
| Prompt injection | Log and file content fenced and marked untrusted; it cannot close its own block |
| Secrets | Tokens masked, signed log URLs never logged, `.env` never in the image |
| Web page | Password-protected, HTML-escaped, strict CSP, no caching, CSRF token on the only form |
| API docs | `/docs` and `/openapi.json` off by default |

### Confidence policy, enforced in code

| Situation | Maximum |
|---|---|
| A log line proves the cause | **0.95** (never 1.0) |
| The cause is inferred, or a quote failed verification | **0.89** |
| Evidence insufficient, or category `unknown` | **0.50** |
| Answer from a team rule | **0.90** |

---

## Configuration

Everything lives in `.env`. Full list with comments: `.env.example`.

**GitHub**

| Variable | Default | Purpose |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | — | **Required.** Without it every delivery is rejected |
| `GITHUB_TOKEN` | — | Read-only fine-grained token |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` | — | GitHub App instead of a token |
| `GITHUB_API_URL` | `https://api.github.com` | Change for GitHub Enterprise Server |
| `GITHUB_LOG_MAX_BYTES` | `200000` | Log kept per failed job |
| `GITHUB_MAX_FAILED_JOB_LOGS` | `5` | Logs fetched when many jobs fail |

**AI**

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama`, `anthropic`, `openai`, `vllm` |
| `LLM_MODEL` | — | Overrides the provider's model |
| `LLM_TIMEOUT_SECONDS` | `300` | One AI call |
| `LLM_MAX_RETRIES` | `2` | Connection errors, rate limits, 5xx |
| `OLLAMA_MODEL` / `OLLAMA_BASE_URL` | gemma-4-31b / host | Local model |
| `OLLAMA_NUM_CTX` | `16384` | Context window; oversized prompts are refused, not truncated |
| `OLLAMA_KEEP_ALIVE` | `2m` | Unload the model after use (shared GPUs) |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | — / `claude-opus-5` | Anthropic |
| `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` | — / `gpt-4o` / — | OpenAI or compatible |
| `DEVINVESTIGATOR_GPU` | `0` | GPU for the bundled Ollama container |

**Investigations**

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite on a volume | `postgresql+asyncpg://…` for PostgreSQL |
| `AUTO_ANALYZE` | `true` | `false` = collect evidence, skip the AI |
| `ANALYSIS_MODE` | `agent` | `single_pass` = one call, no tools |
| `INVESTIGATION_TIMEOUT_SECONDS` | `900` | One whole investigation |
| `AGENT_MAX_TOOL_CALLS` | `6` | Evidence the AI may request |
| `AGENT_TOOL_BUDGET_CHARS` | `24000` | Total tool output added to the prompt |
| `USE_REPOSITORY_RULES` / `RULES_FILE` | `true` / `.devinvestigator.yml` | Team rules |
| `REUSE_PREVIOUS_RESULTS` / `REUSE_WITHIN_DAYS` | `true` / `30` | Repeated failures |

**Notifications and web page**

| Variable | Default | Purpose |
|---|---|---|
| `NOTIFY_GITHUB_COMMENTS` | `false` | Post the answer as a comment |
| `GITHUB_COMMENT_TOKEN` | — | Separate write token, comments only |
| `PUBLIC_BASE_URL` | — | Link back to the investigation |
| `DASHBOARD_PASSWORD` | — | **Required to enable the web page** |
| `DASHBOARD_USERNAME` | `admin` | Login name |
| `ENABLE_API_DOCS` | `false` | `/docs`, `/openapi.json` |
| `DEVINVESTIGATOR_PORT` | `8012` | Host port |
| `DEVINVESTIGATOR_SUBNET` | `10.211.11.0/24` | Docker network subnet |

---

## Failure handling

| Situation | What happens |
|---|---|
| GitHub 404 / 401 / 403 | Recorded per source; the rest of the evidence is still collected |
| Job logs expired (410) | Noted on that job, other evidence still used |
| No failed jobs | Stated in the prompt; the AI answers `unknown` rather than guessing |
| Huge logs | 200 KB tail → error section → prompt refused if it would exceed the context window |
| AI fails or times out | Investigation marked `failed` with the reason; evidence kept for a retry |
| Duplicate webhook delivery | Recognised, answered `duplicate`, never investigated twice |
| Container restarts mid-investigation | Re-queued at startup and run again |
| Comment cannot be posted | Result already saved; the error is recorded on the investigation |

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| Webhook **401** | Secret differs | Same value both sides, then `docker compose up -d` |
| Webhook **503** | Secret not loaded | `.env` is read at startup only |
| Webhook **405** | URL missing the path | Use `…/webhooks/github` |
| `failed`, "ReadTimeout" | Model too slow, usually a busy GPU | Free the GPU, smaller model, or an API provider |
| `failed`, "404" | Token cannot read that repository | Add the repository to the token |
| Web page **503** | No password | Set `DASHBOARD_PASSWORD`, restart |
| Comment not posted | Token lacks permission | *Pull requests: write*; *Contents: write* for commit comments |
| Nothing arrives | GitHub cannot reach the server | `curl https://your-server/health` from outside |

```bash
docker compose logs -f devinvestigator     # live logs
docker exec devinvestigator python -m app.cli list
```

---

## Inside the project

```
app/
├── api/            webhook + health endpoints
├── core/           settings, logging, webhook signature
├── integrations/
│   └── github/     read-only client, auth, models, failure rules
├── evidence/       collection, error extraction, failure fingerprint
├── agent/          agent loop + five read-only tools
├── analysis/       prompt, schema, quote verification, confidence, team rules
├── llm/            provider interface + ollama / anthropic / openai
├── notifications/  GitHub comment rendering and posting
├── database/       models, session, queries (the queue lives here)
├── web/            web page, quality page, feedback
├── orchestrator.py the worker: queue → evidence → rule/reuse/AI → store → notify
└── cli.py          list, show, queue, retry, preview, analyze, investigate
```

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/pytest -q                                   # 295 tests, no network calls
.venv/bin/uvicorn app.main:app --reload --port 8012
```

**Docker:** one container (plus an optional Ollama one), non-root user, healthcheck, own network
`devinvestigator_net`, host port `8012` → container `8000`, database on the volume
`devinvestigator_data`. New database columns are added automatically at startup.

---

## Status & roadmap

### Built

| Area | Detail |
|---|---|
| Trigger | GitHub Actions webhook, signature verified, duplicates recognised |
| Evidence | Jobs, failed steps, logs, error extraction, commit, changed files |
| AI | Local (Ollama) or API (Anthropic, OpenAI, vLLM), provider-independent core |
| Agent | Five read-only tools, budgets, repeat-request guard |
| Trust | Quote verification, schema enforcement, confidence ceilings |
| Speed | Team rules and repeat reuse answer in ~1 second with no AI call |
| Delivery | GitHub comment, web page, quality page, feedback, CLI |
| Operations | Database queue, restart recovery, timeouts, additive column upgrades |
| Tests | 295, no network calls |

### Not built yet

| Item | Why it matters |
|---|---|
| Slack notifications | Team channel alerts |
| Full database migrations | New columns are added automatically; other changes are not |
| Data retention | Logs are stored forever; the database grows |
| Repository allowlist | Needed before sharing the webhook URL widely |
| PostgreSQL testing | Supported in code, never run against a real server |
| GitLab / Jenkins | Optional integrations |
| Concurrency | One investigation at a time (right for a GPU, wasteful with an API) |

### Known limits

- One investigation at a time; 35–110 s each with a local model.
- A shared GPU can slow a model enough that investigations time out.
- GitHub App authentication is implemented and unit-tested, never run against real GitHub.
- The AI is not always right: verify before acting. That is why every quote is checked and the
  confidence is capped.
