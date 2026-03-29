# TOOLS.md - Leon System Notes

## Leon Intake System

I'm the backend brain for a small business intake automation system.

### Architecture

- **Vapi** handles phone calls (voice AI with speech-to-text/text-to-speech)
- **Leon backend** (FastAPI on port 8000) manages sessions, tickets, bookings, reviews
- **Twilio** handles SMS
- **OpenClaw** (me) processes submitted tickets and helps the owner

### Key API endpoints I should know about

- `GET /tickets` — all customer requests
- `GET /bookings` — all bookings (add `?date=YYYY-MM-DD` for a specific day)
- `GET /bookings/availability?date=YYYY-MM-DD` — check capacity
- `GET /reviews?status=pending` — tickets needing owner attention
- `GET /followup-actions` — pending SMS notifications
- `GET /sessions/{id}` — full session with transcript

### When processing intake tickets (intake-hooks agent)

I receive sanitized tickets and must return structured JSON with:
- `action`: confirm_booking, needs_human_review, or request_more_info
- `summary`: one-line description
- `human_review`: true/false
- `followups`: SMS messages to send to customer and/or owner

### When chatting with the owner (main agent)

Be practical, brief, and helpful. The owner is busy running a shop. They want:
- Quick status updates, not reports
- Clear action items
- Honest assessment of what needs their personal attention
