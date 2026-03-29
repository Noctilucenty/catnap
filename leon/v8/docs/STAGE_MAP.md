
# Stage Map (Current)

## Stage 0
Working intake backend + SQLite + OpenClaw webhook bridge.

## Stage 1
Conversation contract layer:
- backend tells front end the current field, missing fields, next prompt, prompt ID, and whether submit is allowed

## Stage 2
Front-end AI draft filling:
- front end proposes candidate fields
- backend validates and merges

## Stage 3
Telephony / SMS skeleton:
- Twilio voice inbound / status
- Twilio SMS inbound
- missed-call follow-up actions

## Stage 4
OpenClaw action contract:
- next action
- human review
- manager escalation
- SMS follow-up policy
