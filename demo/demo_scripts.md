# Demo chat scripts

## A. CardCare (AI agent) chats

These are Person A's 11 scripted chats in `person_a/phase1/cases.json`:
- **F1–F5** each trigger one v1 weakness.
- **G1–G6** must keep working on every version.

The mock bank holds exactly the customers they use (C1001–C1006, Tarang Bank). Show any chat with `python3 cases.py show F3`.

| Chat | Customer | v1 does (wrong) | v2 does |
| --- | --- | --- | --- |
| F1 stolen card | Ananya Rao, C1003 | Blocks without verifying | Verifies, then blocks |
| F2 declined at Croma | Priya Sharma, C1001 | Transfers to a human | Explains: incorrect PIN |
| F3 Dubai trip | Vikram Iyer, C1004 | "Domestic use only" | Offers to enable international usage |
| F4 where's my new card | Rahul Mehta, C1002 | Orders a second card | Dispatched, arriving 10 Nov |
| F5 cracked card | Sneha Kulkarni, C1005 | "About 2 days" | 7 working days (13 Nov), and logs the promise |

## B. Human-agent chat (Promise Keeper)

Paste this into the console under Promise Keeper → "Add a conversation". Use agent type **Human** and customer **Priya Sharma**. Or click "Load the two demo conversations".

> **Agent:** Hi, this is Ravi from Tarang Bank. How can I help you today?
> **Customer:** Hi Ravi. My card was declined at Croma yesterday, and there's a ₹2,350 Swiggy charge on my card that I don't recognise.
> **Agent:** I'm sorry about that, Priya. The Croma payment was declined because of an incorrect PIN. I've raised a dispute for the Swiggy charge. I'll call you back on Thursday with the outcome, and I'll email you the dispute form today.

Expected: two promises, a callback due Thursday and a document due today.

**Then:** click "+3 days" on the demo clock. Both promises turn **Broken**.

**The customer comes back.** The Agent card tab for Priya reads *You promised Priya "…call you back on Thursday…". It hasn't happened. Apologise first.*

## C. If Talkdesk traffic isn't flowing

In the Mender tab, "Replay v1 chats" runs F1–F5 through the v1 agent and logs them as if they came from Talkdesk. It makes no model calls. The rest of the demo works unchanged.
