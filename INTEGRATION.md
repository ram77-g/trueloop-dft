# Integrating TrustLoop with Talkdesk

The order below is the order to do it in. Steps 1–3 are the core and give a working, Talkdesk-integrated demo on their own. Steps 4–8 deepen the integration.

We haven't seen Talkdesk's New MCP Server form or the Workspace Designer card editor yet (Phase 0 questions 1 and 3). Where a field name is a guess, it's marked "(check)". Every step has a fallback that keeps the demo working.

## 0. Get a public URL and check it

```bash
cd trustloop
set GEMINI_API_KEY=...               # PowerShell: $env:GEMINI_API_KEY="..."
uvicorn trustloop.app:create_app --factory --port 8000
ngrok http 8000                      # or deploy to Render with render.yaml
python scripts/check_deployment.py https://YOUR-HOST
```

Expect `ALL GOOD`, with 9 tools over both `/mcp` and `/sse`. From here on, `https://YOUR-HOST` means that URL.

**Auth:** leave `TRUSTLOOP_API_KEY` empty unless Talkdesk's MCP form lets you add a header. If it does, set the key and add the header `Authorization: Bearer <key>` (or `X-API-Key: <key>`) in the form.

## 1. Register the MCP server

1. Go to AI Agent Platform → **MCP Servers** → **New MCP Server**.
2. Fill in the form:
   - **Name:** `TrustLoop_CardCare_MCP_DFT`
   - **Description:** "TrustLoop (Team DFT): Nimbus Bank debit-card tools for CardCare. Every call is logged for Promise Keeper and Mender."
   - **URL:** `https://YOUR-HOST/mcp`
   - **Transport (check):** Streamable HTTP. If the form only offers SSE, use `https://YOUR-HOST/sse`.
   - **Auth (check):** none, or the header from step 0.
3. Save. If the form has a "test" or "refresh tools" button, it should list 9 tools:
   - `verify_customer`
   - `get_card_status`
   - `get_recent_declines`
   - `block_card`
   - `request_replacement`
   - `set_international_usage`
   - `record_promise`
   - `record_outcome`
   - `get_customer_promises`

   The status should show **Active**, like the other teams' servers.

## 2. Create the CardCare agent

1. Go to AI Agent Platform → **AI Agent Orchestration** → **New Orchestration**.
   - **Name:** `CardCare_DFT`
   - **Description:** "TrustLoop demo agent: debit card support for Nimbus Bank. Verifies customers, blocks or replaces cards, explains declines, and logs every promise it makes."
2. Paste the instructions from `agent/cardcare_v1_instructions.md` **exactly**. The weaknesses are deliberate: Mender fixes them live.
3. Attach `TrustLoop_CardCare_MCP_DFT` and enable all 9 tools.
4. If the instructions editor can insert a conversation or interaction ID variable (check), add this line: "Pass `<that variable>` as `session_id` on every tool call." Without it, TrustLoop groups calls by phone number in 30-minute windows, which is fine for the demo.
5. Publish **V1**. Use the orchestration's own test chat to send script B1 from `demo/demo_scripts.md`. In the console's **Live tool log** tab you should see the calls arrive.

## 3. Put CardCare on a customer channel (Studio)

1. Go to Studio → Flows → **Create flow**, choose chat, and name it `CardCare_Chat_DFT`.
2. Mirror `WellNest_Chat_Flow_TeamXYZ` ("Autopilot first, human on escalation"):
   - start;
   - the AI agent step pointing at `CardCare_DFT`;
   - on escalation, assign to a human queue;
   - end.
3. Publish. Use the flow's chat channel (Admin → Channels, or the flow's test link) to run the CardCare scripts B1–B5.

**Fallback:** the orchestration's test chat in step 2 is enough for the demo.

## 4. Show the agent card in the workspace

Endpoints:
- the card's data: `GET https://YOUR-HOST/api/card?phone=<contact phone>`
- a ready-made page: `https://YOUR-HOST/card?phone=<contact phone>` (no auth needed, refreshes every 15 s)

Options, best first:
1. **Workspace Designer card driven by Automation Designer** (the type "Workspace Designer Cards" already exists in the sandbox). Make an HTTP GET to the card endpoint with the contact's phone number and map the fields:
   - `headline`: big text, red when `broken` isn't empty;
   - `broken[].description` and `broken[].due_at`: list;
   - `open[].description` and `open[].due_at`: list.
2. **Panel or Canvas with a URL or iframe (check):** point it at `https://YOUR-HOST/card?phone={{contact phone}}`.
3. **Fallback:** the console's **Agent card** tab shows the same view. CardCare can also call `get_customer_promises` itself, so the AI agent acknowledges broken promises in chat.

## 5. Get human-agent conversations into Promise Keeper

CardCare's own promises already arrive through the `record_promise` tool. Human-agent conversations need one of these:

1. **Automation Flow on conversation end (check the trigger):** an HTTP POST to `https://YOUR-HOST/api/webhooks/talkdesk` with a body like:
   ```json
   {"contact_phone": "+919845001009", "interaction_id": "<id>", "agent_name": "<agent>",
    "messages": [{"role": "customer", "text": "..."}, {"role": "agent", "text": "..."}]}
   ```
   A plain `{"text": "Agent: ...\nCustomer: ...", "contact_phone": "..."}` body works too.
2. **Talkdesk API:** if Phase 0 found a transcript scope, a small poller can call our `POST /api/transcripts`. This isn't built; it takes about 20 minutes once we know the endpoint.
3. **Fallback:** paste the transcript into the console under Promise Keeper → "Add a conversation".

## 6. Send real reminders and customer messages (optional)

1. Create an Automation Flow with a **webhook trigger** and an SMS or email action, using the payload fields `body`, `phone`, `email` and `purpose`.
2. Set `TRUSTLOOP_WEBHOOK_URL` to the trigger URL and restart.

Customer messages (apologies for broken promises, "done as promised" confirmations) then post to it once each. Without it they show as queued in the console.

## 7. Use Talkdesk Agent Evaluation for the gate

1. In the console's Mender tab, run Build cases, then **Download cases (CSV)**.
2. In AI Agent Platform → Agent Evaluation → **Datasets**, create a dataset from that CSV. The format is unverified (Phase 0 question 7), so if import fails, enter the 16 cases by hand. Each row has the customer message and the expected behaviour.
3. Run **Create Evaluation** on `CardCare_DFT` V1 and on the V2 draft.
4. Feed Talkdesk's pass/fail results to the gate:
   ```bash
   curl -X POST https://YOUR-HOST/api/mender/patches/<id>/external-results -H "content-type: application/json" \
     -d '{"before": {"F-policy_breach-1": false, "G1-card-status": true}, "after": {"F-policy_breach-1": true, "G1-card-status": true}}'
   ```

**Fallback:** the console's **Evaluate** runs the same cases with Gemini (or Claude) playing CardCare, against an isolated copy of the bank.

## 8. Ship the fix

1. In the Mender tab, when the gate shows **PASS**, click **Approve**. The newest instructions version appears at the bottom of the tab.
2. In `CardCare_DFT`, create a new draft version, paste the approved instructions and publish **V2**.
3. Rerun scripts B1 and B5. CardCare now verifies before blocking and promises 5–7 working days.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Talkdesk shows the MCP server as errored, or lists no tools | Run `check_deployment.py`. If `/mcp` fails but `/sse` works, register `/sse`. If Render was asleep, open `/health` first. |
| `401 Missing or wrong API key` | Set the same key in Talkdesk's header field, or unset `TRUSTLOOP_API_KEY`. |
| Tool calls appear under `auto-…` sessions | Expected without a session ID (step 2.4). Failures are still captured. |
| The console says "Offline engine" | `GEMINI_API_KEY` isn't set in the server's environment (on Render: Environment tab, then redeploy). |
| Demo state is messy | Click "Reset the whole demo" in the console's Promise Keeper tab (or `POST /api/demo/reset`). |
