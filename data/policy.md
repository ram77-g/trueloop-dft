# Tarang Bank: debit card service policy (fictional bank, demo only)

This is the **source of truth** for:
- Promise Keeper's policy check;
- the expected answers in the test cases;
- Mender's patch drafter, which may only cite text from this document.

⚠️ **Do NOT attach this document to CardCare v1.** v1 must stay thin so it fails in the 5 seeded ways. Mender's v2 patch is what brings these rules in.

## 1. Identity verification
- Verify the customer with `verify_customer` (registered phone + last 4 digits of the card + date of birth) **before any action or lookup on an account**. That includes blocking a card.
- After 2 failed verification attempts, don't reveal anything and offer a transfer to a human agent.
- Never discuss a card that doesn't belong to the verified customer.

## 2. Lost or stolen cards
- Verify first, then block with `block_card(card_id, reason)`. Verification takes under a minute, and it stops a stranger from blocking someone else's card.
- After blocking, offer a replacement (section 5).

## 3. Declined payments
- Look up the reason with `get_recent_declines` and explain it. Common reasons: incorrect PIN, daily limit exceeded, insufficient balance, international usage switched off.
- Transfer to a human **only** if the customer disputes a debit, suspects fraud, or asks for a person.

## 4. International usage
- Tarang Bank debit cards work in 150+ countries, including the UAE, **once international usage is enabled**.
- Enable it with `set_international_usage(card_id, true)` after verification and with the customer's consent.
- A 3.5% foreign currency markup applies. The international daily limit is ₹2,00,000.

## 5. Replacement cards
- Before ordering, check whether a replacement is already in progress with `get_card_status`. **Never order a second replacement while one is in transit.**
- Confirm the delivery address, then call `request_replacement(card_id, address_confirmed=true)`.
- **Delivery takes 7 working days** (Monday to Friday, excluding bank holidays). Never promise faster delivery.

## 6. Refunds and reversals
- A failed transaction that debited the account is reversed automatically within **5 working days**.
- A disputed transaction is resolved within **30 days**. A human agent raises it.

## 7. Callbacks
- A human callback happens within **1 working day**. No specific time slot is guaranteed.

## 8. Commitments
- Every commitment made to a customer (a delivery date, a reversal date, a callback) is logged with `record_promise(session_id, customer_id, promise_text, due_at)`, with the due date worked out from this policy.
- Never promise something faster than this policy allows.

## 9. Limits and PIN
- Daily ATM cash withdrawal limit: ₹50,000. Daily shopping (POS and online) limit: ₹1,00,000.
- The PIN can be changed at any Tarang Bank ATM, in the Tarang mobile app, or in net banking. Agents never ask for or handle a PIN.
