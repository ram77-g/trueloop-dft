# Integrating TrustLoop with Talkdesk

**Live server:** https://trustloop-dft.onrender.com
**MCP endpoint:** `https://trustloop-dft.onrender.com/mcp` (streamable HTTP). If the form only offers SSE, use `/sse`.

The Talkdesk side is Person A's `CardCare_DFT` orchestration: a supervisor plus 4 specialists, built from `person_a/phase1/orchestrator/` with `orchestrator.py`. This server implements the 8-tool contract in that file exactly. A test (`tests/test_mcp_tools.py::test_contract_matches_person_a_orchestrator`) fails if the two ever drift apart.

## 0. Wake the server and check it

The free Render plan sleeps after about 15 idle minutes. Open https://trustloop-dft.onrender.com/health and wait for `"ok":true`. Then, optionally:

```bash
python scripts/check_deployment.py https://trustloop-dft.onrender.com   # expect ALL GOOD
```

## 1. Register the MCP server

1. Go to AI Agent Platform → **MCP Servers** → **+ New MCP Server**.
2. Fill in the form:
   - **Name:** `CardCare_MCP_DFT`
   - **URL:** `https://trustloop-dft.onrender.com/mcp`
   - **Auth:** none
3. Save and wait for **Active**. It should list 9 tools: the 8 in the contract plus `get_customer_promises` (optional).
4. Find the server's **ID** (a UUID). Try the browser address bar first. If it isn't there, attach one tool by hand, save, use **Export Orchestrator**, and look for `mcp_server_id`.

## 2. Import CardCare_DFT with the tools attached

```bash
cd person_a/phase1/orchestrator
python3 orchestrator.py build --mcp-url https://trustloop-dft.onrender.com/mcp --mcp-server-id <uuid>
```

In Talkdesk, open the orchestration → **⋯ → Import Orchestrator** → choose `import/CardCare_DFT_v1.json` → **Save** → **Publish**.

**Recommended:** in `orchestrator.py`, add an optional `session_id` parameter bound to **Interaction ID** to every tool, not just `record_promise` and `record_outcome`. Then every tool call in a chat groups under one session. Without it, TrustLoop groups calls by customer in 30-minute windows, which still works for the demo.

## 3. Run the test chats

Person A's 11 chats are in `person_a/phase1/cases.json`. The mock bank contains exactly their 6 customers (plus 10 extras).

- **On v1:** F1–F5 should fail and G1–G6 should pass.
- **Watch it happen:** the console's **Live tool log** tab shows every MCP call as it arrives.
- **Export for Mender:** `python3 cases.py export --version v1`, then paste `export/transcripts_v1.json` into the console under Mender → **Import Person A's chats**.

## 4. Mender: fix CardCare

In the console's Mender tab:
1. **Capture failures.** Expect 5 clusters.
2. **Build cases.** These are Person A's 11 chats.
3. **Draft fix.**
4. **Evaluate.** Expect **PASS, 55 → 100**. This uses the simulator by default; tick "Use the live model" only for the final run, since it costs about 60–100 Gemini calls.
5. **Over-eager patch.** Expect **BLOCKED**.
6. **Approve** the fix.
7. **Download cardcare_v2.spec.json** from the Agent instructions section.

Then build and import v2:

```bash
python3 orchestrator.py build --spec spec/cardcare_v2.spec.json --version v2 \
  --mcp-url https://trustloop-dft.onrender.com/mcp --mcp-server-id <uuid>
```

Import it the same way, then rerun the chats. **On v2 every chat should pass.**

**Optional:** gate on Talkdesk's own Agent Evaluation results instead of ours by posting them to `POST /api/mender/patches/<id>/external-results` with `{"before": {"F1": false, ...}, "after": {"F1": true, ...}}`.

## 5. Promise Keeper

- **CardCare's promises** arrive through `record_promise`, once v2 tells it to log them.
- **v1's "about 2 days" promise** is caught from the chat transcript (the import in step 3).
- **Human-agent chats:** paste the transcript into the console under Promise Keeper → **Add a conversation**.
- **The agent card:** the console's Agent card tab, or embed `https://trustloop-dft.onrender.com/card?phone=<phone>` in a Workspace Designer card if one can show a URL.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| MCP server not Active, or no tools listed | Open `/health` first (Render may be asleep). If `/mcp` still fails, register `/sse`. |
| A tool returns "No card or customer matches that id" | The agent passed something other than the `customer_id`/`card_id` that `verify_customer` returned. Check the specialist's instructions. |
| The console says "Offline engine" | `GEMINI_API_KEY` isn't set on Render (Environment tab, then redeploy). |
| Demo state is messy | Click "Reset the whole demo" in the console's Promise Keeper tab. |
