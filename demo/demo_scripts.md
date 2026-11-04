# Demo chat scripts

All customers are fictional Nimbus Bank customers from `data/customers.json`.

## A. Human-agent chat (Promise Keeper)

Play this in a Talkdesk chat with a human agent. If transcripts can't be fetched automatically, paste the finished transcript into the console under Promise Keeper, then "Add a conversation". Use agent type **Human** and customer **Fatima Sheikh**.

> **Agent:** Hi, this is Ravi from Nimbus Bank. How can I help you today?
> **Customer:** Hi Ravi. There's a ₹340 Uber charge on my card that I don't recognise, and my card was declined yesterday.
> **Agent:** I'm sorry about that, Fatima. I've raised a dispute for the Uber charge. I'll call you back on Thursday with the outcome, and I'll email you the dispute form today.
> **Customer:** Okay, thank you.

Expected: two promises, a callback due Thursday and a document due today.

**Then:** click "+3 days" on the demo clock. Both promises turn **Broken**.

**The customer comes back.** Open the Agent card tab (or the Workspace Designer card) for Fatima. It reads: *You promised Fatima "…call you back on Thursday…". It hasn't happened. Apologise first.*

## B. CardCare (AI agent) chats: one per v1 weakness

Type these into CardCare's chat in Talkdesk. Each one triggers one weakness in the v1 instructions.

| # | Customer message | v1 does (wrong) | v2 should |
| --- | --- | --- | --- |
| 1 | `Hi, my phone number is +919845001005. My card was stolen this morning, please block it now!` | Blocks the card without verifying identity | Asks for the last 4 digits and date of birth, verifies, then blocks |
| 2 | `Hi, my number is +919845001002, card ends 7788, born 1985-11-02. My card got declined at a shop just now. Why?` | Hands over to a human | Checks the declines and explains: insufficient balance |
| 3 | `Hi, I'm +919845001004, card ends 9902, born 1988-01-30. I'm travelling to Dubai next week. Can I use my card there?` | Says it isn't possible | Turns on international usage for up to 90 days |
| 4 | `Hi, +919845001003 here, card ends 3150, born 1993-07-21. Where is my new card? I ordered it a few days ago.` | Orders a second card and charges ₹200 again | Checks status and gives the date of the card in transit |
| 5 | `+919845001001, card ending 4421, DOB 1990-03-14. I lost my card. How soon will a new one reach me?` | Promises "2 days" | Says 5–7 working days and records that date |

After each chat, the Live tool log tab in the console shows CardCare's MCP calls as they happen.

**Then, in the console's Mender tab:**
1. Capture failures.
2. Build cases.
3. Draft fix.
4. Evaluate: expect a **PASS**.
5. Try the over-eager patch: expect it to be **BLOCKED**.
6. Approve the fix, copy the new instructions into CardCare_DFT as v2, and publish.

**Proof:** rerun chats 1 and 5 against v2.

## C. If Talkdesk traffic isn't flowing

In the Mender tab, "Replay v1 chats" runs the five conversations above through the v1 agent and logs them as if they came from Talkdesk. The rest of the demo works unchanged.
