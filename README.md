# TrustLoop

TrustLoop makes sure every promise made to a customer, by a human or an AI agent, is actually kept. It flags promises that break policy and turns AI agent failures into tested, gated fixes.

This repository is the backend for the Talkdesk Hackathon @ PESIT (team DFT).

## What's inside

| Part | What it does | Where |
| --- | --- | --- |
| **CardCare MCP server** | The 9 tools Talkdesk's AI agent (CardCare) calls, over streamable HTTP (`/mcp`) and SSE (`/sse`). Every call is logged per session. | `trustloop/mcp_tools.py`, `trustloop/bank.py` |
| **Promise Keeper** | Extracts promises from transcripts, checks them against policy, tracks them (open, due soon, kept, broken, can't be kept), queues reminders and customer messages, and powers the agent card. | `trustloop/promises.py`, `trustloop/dates.py`, `trustloop/policy.py` |
| **Mender** | Captures CardCare's failures, clusters them, builds test cases, drafts a patch, evaluates v1 against v2, and gates the release. | `trustloop/mender.py`, `trustloop/evaluator.py` |
| **Console** | One page that drives the demo: dashboard, demo clock, agent card preview, Mender steps, live tool log. | `trustloop/static/index.html`, served at `/` |

**An LLM** extracts promises, diagnoses transcripts, drafts patches and plays the agent during evaluation. Set `GEMINI_API_KEY` to use Gemini (the newest stable Flash model your key can use) or `ANTHROPIC_API_KEY` to use Claude (`claude-opus-5-5`). Both use structured JSON output. Without a key, every one of these has a deterministic offline path:
- rule-based extraction;
- rule-based diagnosis;
- rule-based patches;
- a simulated agent that follows the rules it can read in the instructions.

The same offline path catches any failed API call, so the demo can't die on an API error.

## Run it

```bash
pip install -r requirements-dev.txt
python -m pytest -q                   # 83 tests, no API key or network needed
uvicorn trustloop.app:create_app --factory --port 8000
# console: http://localhost:8000      MCP: http://localhost:8000/mcp (or /sse)
```

Set `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY`) to use a real model. Other settings are listed in `.env.example`.

## Put it on the internet (Talkdesk must reach it)

**Fastest (laptop, about 2 minutes):** ngrok is already installed on this machine.

```bash
uvicorn trustloop.app:create_app --factory --port 8000
ngrok http 8000          # gives https://<random>.ngrok-free.app
python scripts/check_deployment.py https://<random>.ngrok-free.app
```

The laptop must stay awake and online during the demo.

**Steadier (Render, about 10 minutes):**
1. Push this folder to a GitHub repo.
2. On Render, New → Blueprint, and pick the repo. It reads `render.yaml`.
3. Set `GEMINI_API_KEY` when Render asks for it.
4. Run `scripts/check_deployment.py` against the Render URL.

The free tier sleeps after inactivity (the first request takes about 50 s) and its disk resets on redeploy. Open `/health` before the demo to wake it.

## Integrating with Talkdesk

Step by step, with a fallback for each step: [INTEGRATION.md](INTEGRATION.md). Chat scripts for the demo: [demo/demo_scripts.md](demo/demo_scripts.md).

## API (all JSON)

**Promise Keeper**

| Method and path | Purpose |
| --- | --- |
| `POST /api/transcripts` | `{text, agent_name, agent_type: human\|ai, phone, session_id?, started_at?}`: extract promises |
| `POST /api/webhooks/talkdesk` | Same, but accepts `{messages:[{role,text}], contact_phone, interaction_id, agent_type}` from an automation |
| `GET /api/promises?status=&customer_id=&agent_type=` | List promises |
| `POST /api/promises/{id}/kept` | Mark a promise kept |
| `POST /api/promises/{id}/dismiss` | Dismiss a promise |
| `GET /api/card?phone=` | Agent card data |
| `GET /card?phone=` | Agent card as an embeddable page |
| `GET /api/dashboard` | Dashboard figures |
| `GET /api/messages` | Queued reminders and customer messages |
| `GET, POST /api/clock` | Read the demo clock, or move it with `{advance_hours}` / `{reset:true}` |
| `POST /api/events` | Simulate a bank event: `{customer_id, kind: callback_completed\|document_sent\|refund_paid\|replacement_delivered}` |

**Mender**

| Method and path | Purpose |
| --- | --- |
| `POST /api/mender/simulate-traffic` | Replay v1's weak spots into the live log |
| `POST /api/mender/capture` | Capture failures |
| `GET /api/mender/clusters` | Failure clusters |
| `POST /api/mender/cases` | Build cases |
| `GET /api/mender/cases.csv` | Cases as CSV |
| `POST /api/mender/patches` | Draft a patch: `{kind: fix\|overeager}` |
| `POST /api/mender/patches/{id}/evaluate` | Run the evaluation and the gate |
| `POST /api/mender/patches/{id}/external-results` | Gate on Talkdesk Agent Evaluation results: `{before:{case_id:bool}, after:{…}}` |
| `POST /api/mender/patches/{id}/approve` | Approve a patch that passed |

**Other:** `GET /api/tool-calls` (live MCP log), `POST /api/demo/reset`, `POST /api/demo/load-samples`, `GET /health`.
