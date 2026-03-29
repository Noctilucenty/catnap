
# Intake Service Starter v8

This version moves the project closer to the working blueprint:

- phone / SMS / dashboard act like a **front-end conversation AI form guide**
- the backend stays authoritative for session state, validation, ticket merge, and submit
- OpenClaw stays behind the backend as the orchestration / brain layer

## What's new in v8

- `GET /sessions/{id}/state` returns the conversation contract the front end should follow
- prompt library with stable prompt IDs like `intro_01`, `ask_phone_01`, `confirm_01`
- explicit phone collection state when a number is missing
- Twilio voice / SMS skeleton now returns the same front-end contract ideas: `prompt_id`, `next_prompt`, `allow_submit`
- dashboard shows the contract layer so you can build a real phone/SMS front end against it

## Quick start

```bash
cp .env.example .env
./scripts/run_dev.sh
```

Open:

```text
http://127.0.0.1:8000/dashboard
http://127.0.0.1:8000/docs
```

## Core contract idea

The front end should ask the backend what comes next.

Example:

```http
GET /sessions/{id}/state
```

Response shape:

```json
{
  "state": "collect_phone",
  "current_field": "phone",
  "missing_fields": ["phone", "service", "preferred_time", "notes"],
  "next_prompt": "What phone number should we use to text you updates?",
  "prompt_id": "ask_phone_01",
  "play_audio_id": "ask_phone_01",
  "allow_submit": false
}
```

That means your phone or SMS front end can:

1. play a fixed prompt or TTS line
2. listen for the user response
3. propose extracted fields to the backend
4. ask the backend what field is next
5. only submit when the backend says the form is ready
