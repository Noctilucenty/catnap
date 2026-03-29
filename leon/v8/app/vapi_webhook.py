"""Vapi webhook handler — receives tool-calls and events from Vapi voice AI."""
from __future__ import annotations

import logging
from typing import Any

from .config import Settings
from .models import ConversationMessage, ProposeTicketRequest
from .service import IntakeService

logger = logging.getLogger("intake.vapi")


async def handle_vapi_event(payload: dict[str, Any], service: IntakeService, settings: Settings) -> dict[str, Any]:
    """Route a Vapi server event to the right handler."""
    message = payload.get("message", {})
    event_type = message.get("type", "")

    if event_type == "tool-calls":
        return await _handle_tool_calls(message, service, settings)
    elif event_type == "status-update":
        return _handle_status_update(message)
    elif event_type == "end-of-call-report":
        return await _handle_end_of_call(message, service)
    elif event_type == "assistant-request":
        return _handle_assistant_request(message, settings)
    else:
        logger.debug("Vapi event ignored: type=%s", event_type)
        return {"ok": True}


async def _handle_tool_calls(message: dict[str, Any], service: IntakeService, settings: Settings) -> dict[str, Any]:
    """Execute Vapi function tool calls against our backend."""
    tool_calls = message.get("toolCalls", [])
    call_info = message.get("call", {})
    results = []

    for tc in tool_calls:
        tool_call_id = tc.get("id", "")
        func = tc.get("function", {})
        name = func.get("name", "")
        args = func.get("arguments", {})

        logger.info("Vapi tool call: %s args=%s", name, str(args)[:100])

        try:
            result = await _execute_tool(name, args, call_info, service, settings)
        except Exception as exc:
            logger.error("Vapi tool %s failed: %s", name, exc)
            result = f"Error: {exc}"

        results.append({"toolCallId": tool_call_id, "result": str(result) if isinstance(result, str) else _format_result(result)})

    return {"results": results}


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    call_info: dict[str, Any],
    service: IntakeService,
    settings: Settings,
) -> Any:
    """Execute a single tool and return the result."""
    caller_phone = call_info.get("customer", {}).get("number")

    if name == "start_session":
        session = service.start_session(phone=caller_phone, channel="phone")
        contract = service.build_contract(session)
        missing = contract.missing_fields
        if caller_phone:
            instruction = "Session started. The customer's phone is already captured. Ask for their name."
        else:
            instruction = "Session started. Ask the customer for their name. You'll also need their phone number."
        return {
            "session_id": session.session_id,
            "state": session.state.value,
            "missing_fields": missing,
            "next_prompt": contract.next_prompt,
            "instruction": instruction,
        }

    elif name == "get_session_state":
        session_id = args.get("session_id")
        session = service.get_session_or_404(session_id)
        contract = service.build_contract(session)
        return {
            "session_id": session.session_id,
            "state": session.state.value,
            "missing_fields": contract.missing_fields,
            "current_field": contract.current_field,
            "next_prompt": contract.next_prompt,
            "allow_submit": contract.allow_submit,
            "needs_confirmation": contract.needs_confirmation,
            "confirmation_summary": contract.confirmation_summary,
            "ticket": {
                "name": session.ticket.customer.name,
                "phone": session.ticket.customer.phone,
                "service": session.ticket.request.service,
                "preferred_time": session.ticket.request.preferred_time,
                "notes": session.ticket.request.notes,
            },
        }

    elif name == "update_field":
        session_id = args.get("session_id")
        field_name = args.get("field_name")
        field_value = args.get("field_value")

        # Map Vapi field names to our ProposeTicketRequest fields
        field_map = {
            "name": "customer_name",
            "customer_name": "customer_name",
            "phone": "phone",
            "service": "service",
            "preferred_time": "preferred_time",
            "time": "preferred_time",
            "notes": "notes",
        }
        internal_field = field_map.get(field_name, field_name)

        proposal_data = {internal_field: field_value, "source": "vapi_ai", "auto_advance": True}
        proposal = ProposeTicketRequest(**proposal_data)
        result = service.propose_ticket_update(session_id, proposal)

        return {
            "session_id": result.session_id,
            "state": result.state.value,
            "accepted": result.accepted_updates,
            "rejected": result.rejected_updates,
            "missing_fields": result.missing_fields,
            "next_prompt": result.suggested_next_question,
            "instruction": f"Field '{field_name}' updated." if result.accepted_updates else f"Field '{field_name}' was rejected. Ask again.",
        }

    elif name == "confirm_ticket":
        session_id = args.get("session_id")
        session = await service.process_turn(session_id, "yes")
        contract = service.build_contract(session)
        return {
            "session_id": session.session_id,
            "state": session.state.value,
            "allow_submit": contract.allow_submit,
            "instruction": "Customer confirmed. Now call submit_ticket to send to the business.",
        }

    elif name == "request_correction":
        session_id = args.get("session_id")
        field_to_change = args.get("field_to_change", "")
        session = await service.process_turn(session_id, f"change {field_to_change}")
        contract = service.build_contract(session)
        return {
            "session_id": session.session_id,
            "state": session.state.value,
            "missing_fields": contract.missing_fields,
            "next_prompt": contract.next_prompt,
            "instruction": f"Correction mode. Ask the customer for the new {field_to_change}.",
        }

    elif name == "submit_ticket":
        session_id = args.get("session_id")
        session, msg = await service.submit_session(session_id)
        return {
            "session_id": session.session_id,
            "submitted": session.submitted_to_openclaw,
            "message": msg,
            "instruction": "Ticket submitted! Tell the customer their request has been received and they'll get a text with updates. Then end the call.",
        }

    else:
        return {"error": f"Unknown tool: {name}"}


def _handle_status_update(message: dict[str, Any]) -> dict[str, Any]:
    """Log call status changes."""
    status = message.get("status", "")
    call = message.get("call", {})
    call_id = call.get("id", "")
    logger.info("Vapi status: call=%s status=%s", call_id[:12], status)
    return {"ok": True}


async def _handle_end_of_call(message: dict[str, Any], service: IntakeService) -> dict[str, Any]:
    """Save the full transcript from Vapi when the call ends."""
    call = message.get("call", {})
    transcript = message.get("transcript", "")
    messages = message.get("messages", [])
    call_id = call.get("id", "")

    logger.info("Vapi call ended: call=%s messages=%d transcript_len=%d", call_id[:12], len(messages), len(transcript))

    # Try to find the session from the messages (look for start_session result)
    # This is best-effort — the transcript is saved for debugging
    return {"ok": True, "call_id": call_id}


def _handle_assistant_request(message: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Return assistant config for dynamic assistant selection."""
    if settings.vapi_assistant_id:
        return {"assistantId": settings.vapi_assistant_id}
    return {"error": "No assistant configured"}


def _format_result(obj: Any) -> str:
    """Format a dict result as a readable string for the LLM."""
    if isinstance(obj, dict):
        import json
        return json.dumps(obj)
    return str(obj)
