# Leon

AI-powered call and SMS intake system for small businesses. Customers call or text, a voice AI collects their info, the backend processes it, and the shop owner gets notified with actionable recommendations.

Built for restaurants, car repair shops, salons, and any small business that loses customers to missed calls.

## How It Works

```
Customer calls phone number
  → Vapi voice AI picks up, guides through intake
  → Leon backend validates + stores ticket
  → OpenClaw (backend brain) analyzes + recommends action
  → Owner gets SMS notification + dashboard alert
  → Booking created automatically
```

**Three layers:**
- **Vapi** — voice AI that talks to customers naturally (phone calls)
- **Leon backend** — FastAPI service with state machine, validation, booking, review queue
- **OpenClaw** — AI brain that processes tickets and helps the owner make decisions

## Quick Start (Local Development)

### Prerequisites

- Python 3.11+
- [OpenClaw](https://openclaw.ai) installed and running
- [ngrok](https://ngrok.com) for public URL (needed for Vapi webhooks)

### 1. Set up the backend

```bash
cd leon/v8
cp .env.example .env
# Edit .env with your API keys (see Configuration below)
./scripts/run_dev.sh
```

This starts the server on `http://localhost:8000`.

### 2. Start ngrok

```bash
ngrok http 8000
```

Note the public URL (e.g., `https://abc123.ngrok-free.dev`).

### 3. Set up Vapi (voice AI)

```bash
cd leon/v8
python3 scripts/setup_vapi.py
```

This creates the Vapi assistant and assigns a phone number. Follow the prompts.

### 4. Open the dashboard

```
http://localhost:8000/dashboard
```

### 5. Test a call

Call the Vapi phone number or use the Vapi dashboard test call. The intake flow will guide the customer through: name, phone, service, preferred time, notes, confirmation, submission.

## Configuration

Copy `.env.example` to `.env` and fill in:

```bash
# Core
APP_NAME=intake-service
APP_ENV=dev
LOG_LEVEL=INFO
DATABASE_PATH=data/intake.db

# OpenClaw (backend brain)
OPENCLAW_ENABLED=true
OPENCLAW_HOOK_URL=http://127.0.0.1:18789/hooks/agent
OPENCLAW_HOOK_TOKEN=<your openclaw hook token>
OPENCLAW_AGENT_ID=intake-hooks
OPENCLAW_TIMEOUT_SECONDS=60

# LLM (front-end conversation for SMS/web)
LLM_ENABLED=true
GEMINI_API_KEY=<your google api key>
GEMINI_MODEL=gemini-2.5-flash

# Vapi (voice AI for phone calls)
VAPI_ENABLED=true
VAPI_API_KEY=<your vapi api key>
VAPI_ASSISTANT_ID=<created by setup_vapi.py>
VAPI_SERVER_URL=<your ngrok url>/webhooks/vapi

# Twilio (SMS sending — optional for dev)
TWILIO_ENABLED=false
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=

# Owner notifications
OWNER_PHONE=<shop owner phone in +1XXXXXXXXXX format>
OWNER_NOTIFICATIONS_ENABLED=true
```

## Deployment

### Option A: Single VPS (simplest)

1. Get a VPS (DigitalOcean, Linode, etc.) — Ubuntu 22.04+, 2GB RAM is enough
2. Install Python 3.11+, Node 22+ (for OpenClaw)
3. Install OpenClaw: `npm install -g openclaw`
4. Clone the repo and set up:

```bash
git clone <your-repo-url> leon
cd leon/leon/v8
cp .env.example .env
# Edit .env with production values

# Create venv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start OpenClaw
openclaw start

# Start Leon backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

5. Set up a reverse proxy (nginx/caddy) for HTTPS:

```nginx
# /etc/nginx/sites-available/leon
server {
    listen 443 ssl;
    server_name leon.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/leon.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/leon.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

6. Use systemd to keep services running:

```ini
# /etc/systemd/system/leon.service
[Unit]
Description=Leon Intake Service
After=network.target

[Service]
User=leon
WorkingDirectory=/home/leon/leon/leon/v8
Environment=PATH=/home/leon/leon/leon/v8/.venv/bin
ExecStart=/home/leon/leon/leon/v8/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

7. Update Vapi assistant's server URL to your production domain:
```bash
# In the Vapi dashboard or via API
Server URL: https://leon.yourdomain.com/webhooks/vapi
```

### Option B: Docker (coming soon)

### Important for Production

- Use a real domain with HTTPS (Vapi requires HTTPS for webhooks)
- Set `APP_ENV=production` and `LOG_LEVEL=WARNING`
- Set up a Twilio account with a real phone number for SMS
- Import the Twilio number into Vapi for inbound calls
- Configure `OWNER_PHONE` so the owner gets SMS notifications
- Back up the SQLite database regularly (`data/intake.db`)

## Project Structure

```
leon/v8/                    # Main application
  app/
    main.py                 # FastAPI endpoints
    service.py              # Business logic
    state_machine.py        # Conversation state machine
    models.py               # Data models
    database.py             # SQLite repository
    llm.py                  # Gemini Flash (front-end conversation AI)
    extraction.py           # Regex field extraction (LLM fallback)
    openclaw_adapter.py     # OpenClaw submission + response parsing
    vapi_webhook.py         # Vapi voice AI webhook handler
    vapi_config.py          # Vapi assistant configuration
    sms_sender.py           # Twilio SMS sending
    calendar.py             # Booking/availability management
    config.py               # Settings from .env
    templates/
      dashboard.html        # Owner dashboard (single-file app)
  scripts/
    run_dev.sh              # Start dev server
    run_cli.sh              # Interactive CLI
    setup_vapi.py           # One-time Vapi assistant setup

openclaw-workspace/         # OpenClaw agent configs
  workspace/                # Main agent (owner assistant)
  workspace-intake/         # Intake-hooks agent (ticket processor)
```

## API Endpoints

See full API docs at `http://localhost:8000/docs` (Swagger UI).

Key endpoints:
- `POST /sessions` — start intake session
- `POST /sessions/{id}/turn` — process customer message
- `POST /sessions/{id}/submit` — submit to OpenClaw
- `GET /bookings` — list bookings
- `GET /reviews` — list review queue
- `POST /owner/chat` — chat with OpenClaw assistant
- `POST /webhooks/vapi` — Vapi voice AI webhook
- `POST /webhooks/twilio/sms/inbound` — SMS webhook

## License

Private — not open source yet.
