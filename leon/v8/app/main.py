
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from collections import defaultdict
import time

from .config import Settings, get_settings
from .database import SessionRepository, get_connection, init_db

logger = logging.getLogger("intake")
from .models import (
    BookingSummary,
    ConversationContract,
    HealthResponse,
    PromptSpec,
    ProposeTicketRequest,
    ProposeTicketResponse,
    ResolveReviewRequest,
    ReviewItemSummary,
    StartSessionRequest,
    StartSessionResponse,
    SubmitSessionResponse,
    SuggestFromTextRequest,
    SuggestFromTextResponse,
    TicketDetail,
    TicketSummary,
    UserTurnRequest,
    UserTurnResponse,
)
from .service import IntakeService
from .state_machine import PROMPT_LIBRARY


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger.info("Starting %s (env=%s)", settings.app_name, settings.app_env)
    logger.info("Database: %s", settings.database_path)
    logger.info("OpenClaw: enabled=%s url=%s agent=%s", settings.openclaw_enabled, settings.openclaw_hook_url, settings.openclaw_agent_id)
    logger.info("LLM: active=%s model=%s", settings.llm_active, settings.gemini_model)
    logger.info("Twilio: enabled=%s from=%s", settings.twilio_enabled, settings.twilio_from_number or "not set")
    logger.info("Vapi: enabled=%s assistant=%s", settings.vapi_enabled, settings.vapi_assistant_id or "not set")
    conn = get_connection(settings.database_path)
    init_db(conn)
    app.state.db_conn = conn
    app.state.repository = SessionRepository(conn)
    app.state.service = IntakeService(app.state.repository, settings)
    yield
    conn.close()


app = FastAPI(title="Intake Service", version="0.8.0", lifespan=lifespan)


def get_repository() -> SessionRepository:
    return app.state.repository


def get_service() -> IntakeService:
    return app.state.service


def get_app_settings() -> Settings:
    return get_settings()


RepoDep = Annotated[SessionRepository, Depends(get_repository)]
ServiceDep = Annotated[IntakeService, Depends(get_service)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    template_path = Path(__file__).parent / "templates" / "dashboard.html"
    return FileResponse(template_path)


@app.get("/health", response_model=HealthResponse)
def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(status="ok", app_name=settings.app_name, db_path=settings.database_path)


@app.get("/health/deep")
async def health_deep(settings: SettingsDep) -> dict:
    """Check connectivity to Gemini, Twilio, and Vapi."""
    import httpx
    results: dict = {}

    # Gemini check
    if settings.llm_active:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={settings.gemini_api_key}"
                )
                results["gemini"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
        except Exception as e:
            results["gemini"] = f"unreachable:{e}"
    else:
        results["gemini"] = "disabled"

    # Twilio check
    if settings.twilio_enabled and settings.twilio_account_sid:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}.json",
                    auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                )
                results["twilio"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
        except Exception as e:
            results["twilio"] = f"unreachable:{e}"
    else:
        results["twilio"] = "disabled"

    # Vapi check
    if settings.vapi_enabled and settings.vapi_api_key:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    "https://api.vapi.ai/assistant",
                    headers={"Authorization": f"Bearer {settings.vapi_api_key}"},
                )
                results["vapi"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
        except Exception as e:
            results["vapi"] = f"unreachable:{e}"
    else:
        results["vapi"] = "disabled"

    overall = "ok" if all(v in ("ok", "disabled") for v in results.values()) else "degraded"
    return {"status": overall, "checks": results}


# Simple in-memory rate limiter: max 30 turns per session per minute
_rate_limit_store: dict = defaultdict(list)
_RATE_LIMIT_MAX = 30
_RATE_LIMIT_WINDOW = 60  # seconds


def _check_rate_limit(session_id: str) -> None:
    now = time.time()
    window_start = now - _RATE_LIMIT_WINDOW
    calls = _rate_limit_store[session_id]
    # Purge old calls
    _rate_limit_store[session_id] = [t for t in calls if t > window_start]
    if len(_rate_limit_store[session_id]) >= _RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    _rate_limit_store[session_id].append(now)


@app.get("/prompt-library", response_model=list[PromptSpec])
def prompt_library() -> list[PromptSpec]:
    return list(PROMPT_LIBRARY.values())


@app.post("/sessions", response_model=StartSessionResponse)
def start_session(body: StartSessionRequest, service: ServiceDep) -> StartSessionResponse:
    session = service.start_session(phone=body.phone, channel=body.channel)
    return StartSessionResponse(
        session_id=session.session_id,
        state=session.state,
        assistant_message=session.last_assistant_message or service.state_machine.start_message(),
        ticket=session.ticket,
        transcript=session.transcript,
        contract=service.build_contract(session),
    )


@app.get("/sessions/{session_id}", response_model=UserTurnResponse)
def get_session(session_id: str, service: ServiceDep) -> UserTurnResponse:
    session = service.get_session_or_404(session_id)
    return UserTurnResponse(
        session_id=session.session_id,
        state=session.state,
        assistant_message=session.last_assistant_message or service.state_machine.start_message(),
        ticket=session.ticket,
        submitted_to_openclaw=session.submitted_to_openclaw,
        openclaw_response=session.openclaw_response,
        transcript=session.transcript,
        missing_fields=service.state_machine.missing_fields(session.ticket),
        contract=service.build_contract(session),
    )


@app.get("/sessions/{session_id}/state", response_model=ConversationContract)
def get_session_contract(session_id: str, service: ServiceDep) -> ConversationContract:
    session = service.get_session_or_404(session_id)
    return service.build_contract(session)


@app.post("/sessions/{session_id}/turn", response_model=UserTurnResponse)
async def user_turn(session_id: str, body: UserTurnRequest, service: ServiceDep) -> UserTurnResponse:
    _check_rate_limit(session_id)
    session = await service.process_turn(session_id, body.user_input)
    return UserTurnResponse(
        session_id=session.session_id,
        state=session.state,
        assistant_message=session.last_assistant_message,
        ticket=session.ticket,
        submitted_to_openclaw=session.submitted_to_openclaw,
        openclaw_response=session.openclaw_response,
        transcript=session.transcript,
        missing_fields=service.state_machine.missing_fields(session.ticket),
        contract=service.build_contract(session),
    )


@app.post("/sessions/{session_id}/suggest", response_model=SuggestFromTextResponse)
def suggest_ticket_fields(session_id: str, body: SuggestFromTextRequest, service: ServiceDep) -> SuggestFromTextResponse:
    return service.suggest_from_text(session_id, body.text)


@app.post("/sessions/{session_id}/propose", response_model=ProposeTicketResponse)
def propose_ticket_fields(session_id: str, body: ProposeTicketRequest, service: ServiceDep) -> ProposeTicketResponse:
    return service.propose_ticket_update(session_id, body)


@app.post("/sessions/{session_id}/submit", response_model=SubmitSessionResponse)
async def submit_session(session_id: str, service: ServiceDep) -> SubmitSessionResponse:
    session, message = await service.submit_session(session_id)
    return SubmitSessionResponse(
        session_id=session.session_id,
        state=session.state,
        ticket=session.ticket,
        submitted_to_openclaw=session.submitted_to_openclaw,
        openclaw_response=session.openclaw_response,
        message=message,
        transcript=session.transcript,
        contract=service.build_contract(session),
    )


@app.get("/tickets", response_model=list[TicketSummary])
def list_tickets(repository: RepoDep, limit: int = 50) -> list[TicketSummary]:
    return repository.list_tickets(limit=limit)


@app.get("/tickets/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: str, repository: RepoDep) -> TicketDetail:
    detail = repository.get_ticket_detail(ticket_id)
    if not detail:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Ticket not found")
    return detail


@app.get("/followup-actions")
def list_followup_actions(repository: RepoDep, limit: int = 50) -> list[dict[str, Any]]:
    return [action.model_dump(mode="json") for action in repository.list_followup_actions(limit=limit)]


@app.post("/followup-actions/{action_id}/execute")
async def execute_followup_action(action_id: str, service: ServiceDep) -> dict[str, Any]:
    return await service.execute_followup_action(action_id)


@app.post("/followup-actions/execute-pending")
async def execute_pending_followups(service: ServiceDep) -> list[dict[str, Any]]:
    return await service.execute_pending_followups()


# ── Review queue ──


@app.get("/reviews", response_model=list[ReviewItemSummary])
def list_reviews(repository: RepoDep, status: str | None = None, limit: int = 50) -> list[ReviewItemSummary]:
    return repository.list_reviews(status=status, limit=limit)


@app.get("/reviews/{review_id}")
def get_review(review_id: str, repository: RepoDep) -> dict[str, Any]:
    from fastapi import HTTPException

    item = repository.get_review_item(review_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review not found")
    # Include full ticket detail
    ticket_detail = repository.get_ticket_detail(item.ticket_id)
    return {
        "review": item.model_dump(mode="json"),
        "ticket": ticket_detail.model_dump(mode="json") if ticket_detail else None,
    }


@app.post("/reviews/{review_id}/resolve")
def resolve_review(review_id: str, body: ResolveReviewRequest, repository: RepoDep) -> dict[str, Any]:
    from fastapi import HTTPException

    success = repository.resolve_review(review_id, body.resolved_by, body.resolution_notes)
    if not success:
        raise HTTPException(status_code=404, detail="Review not found or already resolved")
    return {"review_id": review_id, "status": "resolved", "resolved_by": body.resolved_by}


# ── Bookings ──


@app.get("/bookings", response_model=list[BookingSummary])
def list_bookings(repository: RepoDep, date: str | None = None, limit: int = 50) -> list[BookingSummary]:
    if date:
        return repository.list_bookings_for_date(date)
    return repository.list_bookings(limit=limit)


@app.get("/bookings/availability")
def check_booking_availability(date: str, repository: RepoDep) -> dict[str, Any]:
    from .calendar import check_availability
    return check_availability(repository, date)


@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, repository: RepoDep) -> dict[str, Any]:
    from fastapi import HTTPException
    success = repository.cancel_booking(booking_id)
    if not success:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"booking_id": booking_id, "status": "cancelled"}


# ── Owner chat (OpenClaw) ──


@app.post("/owner/chat")
async def owner_chat(request: Request, settings: SettingsDep) -> dict[str, Any]:
    import asyncio
    import json as json_mod

    body = await request.json()
    message = body.get("message", "")
    if not message:
        return {"reply": "Please type a message."}
    if not settings.openclaw_enabled:
        return {"reply": "OpenClaw is not enabled."}

    try:
        from .openclaw_adapter import _find_openclaw
        openclaw_bin = _find_openclaw()
        cmd = [
            openclaw_bin, "agent",
            "--agent", "main",
            "--json",
            "--timeout", "30",
            "--session-id", "leon-owner-dashboard",
            "--message", message,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=35)
        if proc.returncode != 0:
            return {"reply": "OpenClaw didn't respond. Try again."}
        data = json_mod.loads(stdout.decode())
        text = data.get("result", {}).get("payloads", [{}])[0].get("text", "No response.")
        # Strip thinking blocks that Gemini sometimes includes
        import re as re_mod
        text = re_mod.sub(r'^think\b.*?(?=\n[A-Z]|\n\n)', '', text, flags=re_mod.DOTALL).strip()
        # Also strip "Thinking Process:" blocks
        if 'Thinking Process:' in text:
            parts = re_mod.split(r'(?:Thinking Process:.*?)(?=\n[A-Z*#])', text, flags=re_mod.DOTALL)
            text = parts[-1].strip() if parts else text
        return {"reply": text}
    except Exception as exc:
        return {"reply": f"Error: {exc}"}


# ── Vapi webhook ──


@app.post("/webhooks/vapi")
async def vapi_webhook(request: Request, service: ServiceDep, settings: SettingsDep) -> dict[str, Any]:
    from .vapi_webhook import handle_vapi_event

    payload = await request.json()
    return await handle_vapi_event(payload, service, settings)


# ── Twilio webhooks ──


@app.post("/webhooks/twilio/voice/inbound", include_in_schema=True)
async def twilio_voice_inbound(request: Request, service: ServiceDep) -> Response:
    payload = await _read_form_or_json(request)
    result = await service.handle_voice_inbound(payload)
    return Response(content=_voice_twiml(result), media_type="application/xml")


@app.post("/webhooks/twilio/voice/gather", include_in_schema=True)
async def twilio_voice_gather(request: Request, service: ServiceDep) -> Response:
    payload = await _read_form_or_json(request)
    result = await service.handle_voice_gather(payload)
    return Response(content=_voice_twiml(result), media_type="application/xml")


@app.post("/webhooks/twilio/voice/status")
async def twilio_voice_status(request: Request, service: ServiceDep) -> dict[str, Any]:
    payload = await _read_form_or_json(request)
    return await service.handle_voice_status(payload)


@app.post("/webhooks/twilio/sms/inbound")
async def twilio_sms_inbound(request: Request, service: ServiceDep) -> Response:
    payload = await _read_form_or_json(request)
    result = await service.handle_sms_inbound(payload)
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{_xml_escape(result['reply'])}</Message></Response>"
    )
    return Response(content=xml, media_type="application/xml")


async def _read_form_or_json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    form = await request.form()
    return dict(form)


def _voice_twiml(result: dict[str, Any]) -> str:
    say = _xml_escape(result["say"])
    if result.get("hangup"):
        return f"<?xml version='1.0' encoding='UTF-8'?><Response><Say>{say}</Say><Hangup/></Response>"
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<Response>"
        "<Gather input='speech' timeout='6' speechTimeout='auto' "
        "action='/webhooks/twilio/voice/gather' method='POST'>"
        f"<Say>{say}</Say>"
        "</Gather>"
        f"<Say>{_xml_escape(result.get('timeout_say', 'I did not hear anything. Goodbye.'))}</Say>"
        "</Response>"
    )


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
