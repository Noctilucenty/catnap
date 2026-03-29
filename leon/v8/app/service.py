from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger("intake.service")

from .config import Settings
from .database import SessionRepository
from .extraction import extract_candidate_fields, extract_phone, sanitize_notes
from .models import (
    CandidateFields,
    ConversationContract,
    ConversationMessage,
    FollowupAction,
    IntakeSession,
    ProposeTicketRequest,
    ProposeTicketResponse,
    ReviewItem,
    SessionState,
    SuggestFromTextResponse,
    TelephonyEvent,
)
from .calendar import check_availability, create_booking_from_session
from .openclaw_adapter import parse_openclaw_response, submit_ticket
from .state_machine import IntakeStateMachine, PROMPT_LIBRARY


class IntakeService:
    def __init__(self, repository: SessionRepository, settings: Settings):
        self.repository = repository
        self.settings = settings
        self.state_machine = IntakeStateMachine()

    def start_session(self, phone: str | None = None, channel: str = "phone") -> IntakeSession:
        session = IntakeSession()
        session.ticket.channel = channel
        if phone:
            session.ticket.customer.phone = phone
        session.state = self.state_machine.resolve_state(session)
        message = self.state_machine.start_message() if session.state == SessionState.COLLECT_NAME else self.state_machine.message_for_state(session)
        session.last_assistant_message = message
        session.transcript.append(ConversationMessage(role="assistant", content=message))
        session.touch()
        self.repository.save_session(session)
        logger.info("Session started: %s channel=%s phone=%s", session.session_id, channel, phone or "none")
        return session

    def get_session_or_404(self, session_id: str) -> IntakeSession:
        session = self.repository.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    def build_contract(self, session: IntakeSession) -> ConversationContract:
        state = self.state_machine.resolve_state(session)
        prompt_id = self.state_machine.prompt_id_for_state(state)
        ticket = session.ticket
        suggested_channel_switch = None
        if ticket.channel == "phone" and (state == SessionState.COLLECT_NOTES or state == SessionState.CONFIRMATION):
            suggested_channel_switch = "sms_optional"
        return ConversationContract(
            session_id=session.session_id,
            state=state,
            channel=ticket.channel,
            current_field=self.state_machine.current_field(session),
            required_fields=self.state_machine.required_fields(),
            missing_fields=self.state_machine.missing_fields(ticket),
            next_prompt=self.state_machine.suggested_next_question(session),
            prompt_id=prompt_id,
            play_audio_id=self.state_machine.play_audio_id_for_session(session),
            allow_submit=state == SessionState.SUBMITTED and not session.submitted_to_openclaw,
            needs_confirmation=state == SessionState.CONFIRMATION,
            draft_form=CandidateFields(
                customer_name=ticket.customer.name,
                phone=ticket.customer.phone,
                service=ticket.request.service,
                preferred_time=ticket.request.preferred_time,
                notes=ticket.request.notes,
            ),
            authoritative_ticket=ticket,
            confirmation_summary=self.state_machine.confirmation_summary(ticket) if state in {SessionState.CONFIRMATION, SessionState.SUBMITTED} else None,
            suggested_channel_switch=suggested_channel_switch,
        )

    async def process_turn(self, session_id: str, user_input: str) -> IntakeSession:
        session = self.get_session_or_404(session_id)
        current_state = session.state
        session.transcript.append(ConversationMessage(role="user", content=user_input))

        message = None
        used_llm = False

        if self.settings.llm_active and session.state not in {
            SessionState.CONFIRMATION,
            SessionState.CORRECTION,
            SessionState.SUBMITTED,
        }:
            from .llm import call_gemini

            missing = self.state_machine.missing_fields(session.ticket)
            llm_result = await call_gemini(session, user_input, missing, self.settings)
            if llm_result is not None:
                regex_phone = extract_phone(user_input)
                if regex_phone and not session.ticket.customer.phone:
                    session.ticket.customer.phone = regex_phone
                self._apply_llm_fields(session, llm_result.extracted_fields)
                session.state = self.state_machine.resolve_state(session)
                session.touch()
                message = self._sanitize_llm_reply(llm_result.reply, session)
                used_llm = message is not None

        if message is None:
            message = self.state_machine.handle_turn(session, user_input)

        session.last_assistant_message = message
        session.transcript.append(ConversationMessage(role="assistant", content=message))
        self.repository.save_session(session)
        logger.info("Turn processed: %s %s->%s llm=%s", session_id, current_state.value, session.state.value, used_llm)
        return session

    def suggest_from_text(self, session_id: str, text: str) -> SuggestFromTextResponse:
        session = self.get_session_or_404(session_id)
        candidates = extract_candidate_fields(text)
        preview = session.model_copy(deep=True)
        self._apply_candidate_updates(preview, candidates, strict=False)
        preview.state = self.state_machine.resolve_state(preview)
        return SuggestFromTextResponse(
            session_id=session.session_id,
            source_text=text,
            candidate_fields=candidates,
            missing_fields=self.state_machine.missing_fields(preview.ticket),
            suggested_next_question=self.state_machine.suggested_next_question(preview),
            preview_ticket=preview.ticket,
        )

    def propose_ticket_update(self, session_id: str, proposal: ProposeTicketRequest) -> ProposeTicketResponse:
        session = self.get_session_or_404(session_id)
        accepted, rejected = self._apply_candidate_updates(session, CandidateFields(**proposal.model_dump(exclude={"source", "raw_text", "auto_advance"})))
        if proposal.raw_text:
            session.transcript.append(
                ConversationMessage(role="system", content=f"Frontend AI draft source: {proposal.raw_text[:200]}")
            )
        if accepted:
            session.transcript.append(
                ConversationMessage(role="system", content=f"Applied draft fields from {proposal.source}: {accepted}")
            )
        if rejected:
            session.transcript.append(
                ConversationMessage(role="system", content=f"Rejected draft fields from {proposal.source}: {rejected}")
            )
        if proposal.auto_advance:
            session.last_assistant_message = self.state_machine.refresh_after_ticket_change(session)
            session.transcript.append(ConversationMessage(role="assistant", content=session.last_assistant_message))
        session.ticket.status = self._local_ticket_status_for_state(session.state)
        session.touch()
        self.repository.save_session(session)
        return ProposeTicketResponse(
            session_id=session.session_id,
            state=session.state,
            ticket=session.ticket,
            accepted_updates=accepted,
            rejected_updates=rejected,
            missing_fields=self.state_machine.missing_fields(session.ticket),
            suggested_next_question=self.state_machine.suggested_next_question(session),
            source=proposal.source,
            contract=self.build_contract(session),
        )

    async def submit_session(self, session_id: str) -> tuple[IntakeSession, str]:
        session = self.get_session_or_404(session_id)
        if session.state != SessionState.SUBMITTED:
            raise HTTPException(status_code=400, detail="Session must reach submitted state before backend submission")
        if session.submitted_to_openclaw:
            self.repository.save_session(session)
            return session, "Ticket was already submitted to OpenClaw."

        logger.info("Submitting ticket %s to OpenClaw for session %s", session.ticket.ticket_id, session_id)
        submission_result = await submit_ticket(session.ticket, self.settings)
        session.submitted_to_openclaw = submission_result.submitted
        session.openclaw_response = submission_result.response
        session.ticket.status = "submitted_to_openclaw" if submission_result.submitted else "confirmed_local"
        session.last_assistant_message = (
            "Thanks. Your request has been submitted. We'll follow up with the next step shortly."
        )
        session.transcript.append(
            ConversationMessage(
                role="system",
                content=f"OpenClaw submission result: {submission_result.submitted}",
            )
        )
        session.transcript.append(ConversationMessage(role="assistant", content=session.last_assistant_message))
        session.touch()
        self.repository.save_session(session)

        if submission_result.submitted:
            logger.info("Ticket %s submitted to OpenClaw successfully", session.ticket.ticket_id)
            booking = create_booking_from_session(session, self.repository)
            if booking:
                session.transcript.append(
                    ConversationMessage(role="system", content=f"Booking created: {booking.booking_id} on {booking.booking_date}")
                )
            await self._handle_openclaw_response(session)
            return session, "Ticket submitted to OpenClaw."

        logger.warning("Ticket %s not submitted to OpenClaw: %s", session.ticket.ticket_id, submission_result.response)
        await self._fallback_owner_notification(
            session,
            reason="Fallback owner notification (OpenClaw submission failed)",
        )
        return session, "OpenClaw disabled or submission failed; fallback owner notification created."

    async def _handle_openclaw_response(self, session: IntakeSession) -> None:
        parsed = parse_openclaw_response(session.openclaw_response)
        if not parsed:
            logger.warning("Could not parse OpenClaw response for session %s", session.session_id)
            await self._fallback_owner_notification(
                session,
                reason="Fallback owner notification (OpenClaw parse failed)",
            )
            return

        ticket = session.ticket
        logger.info("OpenClaw action=%s review=%s followups=%d", parsed.action, parsed.human_review_needed, len(parsed.followups))

        for followup in parsed.followups:
            if followup.type == "notify_owner" and self.settings.owner_phone and self.settings.owner_notifications_enabled:
                action = FollowupAction(
                    session_id=session.session_id,
                    ticket_id=ticket.ticket_id,
                    action_type="notify_owner",
                    channel="sms",
                    destination=self.settings.owner_phone,
                    reason="OpenClaw: owner notification",
                    payload={"suggested_message": followup.message},
                )
                await self._send_and_record_followup(action)

            elif followup.type == "sms_customer" and ticket.customer.phone:
                action = FollowupAction(
                    session_id=session.session_id,
                    ticket_id=ticket.ticket_id,
                    action_type="sms_followup",
                    channel="sms",
                    destination=ticket.customer.phone,
                    reason="OpenClaw: customer notification",
                    payload={"suggested_message": followup.message},
                )
                await self._send_and_record_followup(action)

        if not parsed.followups and self.settings.owner_phone and self.settings.owner_notifications_enabled:
            review_tag = " [REVIEW]" if parsed.human_review_needed else ""
            raw_msg = f"New{review_tag}: {ticket.customer.name} - {ticket.request.service} at {ticket.request.preferred_time}"
            owner_msg = self._truncate_sms_message(raw_msg)
            action = FollowupAction(
                session_id=session.session_id,
                ticket_id=ticket.ticket_id,
                action_type="notify_owner",
                channel="sms",
                destination=self.settings.owner_phone,
                reason="Fallback owner notification",
                payload={"suggested_message": owner_msg},
            )
            await self._send_and_record_followup(action)

        if parsed.human_review_needed:
            review_item = ReviewItem(
                ticket_id=ticket.ticket_id,
                session_id=session.session_id,
                reason=parsed.review_reason or f"OpenClaw: {parsed.summary[:200]}",
                openclaw_summary=parsed.summary,
                openclaw_next_step=parsed.next_step,
            )
            self.repository.save_review_item(review_item)
            logger.info("Created review item: %s reason=%s", review_item.review_id, parsed.review_reason or parsed.action)

        session.transcript.append(
            ConversationMessage(
                role="system",
                content=f"OpenClaw [{parsed.action}]: {parsed.summary} | Review: {parsed.human_review_needed}",
            )
        )
        session.touch()
        self.repository.save_session(session)

    async def _fallback_owner_notification(self, session: IntakeSession, reason: str) -> None:
        if not self.settings.owner_phone or not self.settings.owner_notifications_enabled:
            logger.info("Fallback owner notification skipped: owner notifications disabled or owner phone missing")
            return

        ticket = session.ticket
        raw_msg = f"New request: {ticket.customer.name} - {ticket.request.service} at {ticket.request.preferred_time}"
        owner_msg = self._truncate_sms_message(raw_msg)

        action = FollowupAction(
            session_id=session.session_id,
            ticket_id=ticket.ticket_id,
            action_type="notify_owner",
            channel="sms",
            destination=self.settings.owner_phone,
            reason=reason,
            payload={"suggested_message": owner_msg},
        )
        await self._send_and_record_followup(action)

        session.transcript.append(
            ConversationMessage(role="system", content=f"Created fallback owner notification: {reason}")
        )
        session.touch()
        self.repository.save_session(session)

    async def _send_and_record_followup(self, action: FollowupAction) -> dict[str, Any]:
        from .sms_sender import send_sms

        self.repository.save_followup_action(action)

        if not action.destination:
            self.repository.update_followup_action_status(action.action_id, "failed")
            logger.warning("Followup %s has no destination", action.action_id)
            return {"action_id": action.action_id, "status": "failed", "sms_sid": None, "error": "missing_destination"}

        message = action.payload.get("suggested_message", "We have an update on your request.")
        result = await send_sms(action.destination, message, self.settings)
        new_status = "sent" if result.sent else "pending"
        self.repository.update_followup_action_status(action.action_id, new_status)

        logger.info(
            "Followup %s executed: type=%s status=%s sms_sid=%s",
            action.action_id,
            action.action_type,
            new_status,
            result.sid,
        )
        return {"action_id": action.action_id, "status": new_status, "sms_sid": result.sid, "error": result.error}

    def _truncate_sms_message(self, message: str, max_length: int = 160) -> str:
        if len(message) <= max_length:
            return message
        truncated = message[: max_length - 3]
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated + "..."

    async def handle_voice_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        from_number = normalize_phone(payload.get("From"))
        logger.info("Voice inbound from %s", from_number or "unknown")
        session = self.repository.find_latest_session_by_phone(from_number) if from_number else None
        if not session or session.submitted_to_openclaw:
            session = self.start_session(phone=from_number, channel="phone")
        call_sid = payload.get("CallSid")
        self.repository.save_event(
            TelephonyEvent(
                source="twilio_voice",
                event_type="voice_inbound",
                external_id=call_sid,
                session_id=session.session_id,
                payload=payload,
            )
        )
        contract = self.build_contract(session)
        return {
            "session_id": session.session_id,
            "say": contract.next_prompt,
            "prompt_id": contract.prompt_id,
            "play_audio_id": contract.play_audio_id,
        }

    async def handle_voice_gather(self, payload: dict[str, Any]) -> dict[str, Any]:
        call_sid = payload.get("CallSid")
        speech_result = (payload.get("SpeechResult") or "").strip()
        logger.info("Voice gather: CallSid=%s speech=%s", call_sid, speech_result[:80] if speech_result else "(empty)")

        session_id = self.repository.find_session_id_by_external_id(call_sid) if call_sid else None
        if not session_id:
            logger.warning("Voice gather: no session for CallSid=%s", call_sid)
            return {"say": "Sorry, I lost track of our conversation. Please call back.", "hangup": True}

        if not speech_result:
            session = self.get_session_or_404(session_id)
            contract = self.build_contract(session)
            return {
                "session_id": session_id,
                "say": f"I didn't catch that. {contract.next_prompt}",
                "timeout_say": "I still didn't hear anything. We'll send you a text to continue. Goodbye.",
            }

        session = await self.process_turn(session_id, speech_result)

        if session.state == SessionState.SUBMITTED and not session.submitted_to_openclaw:
            logger.info("Voice auto-submit for session %s", session_id)
            session, _ = await self.submit_session(session_id)
            return {
                "session_id": session_id,
                "say": "Your request has been submitted. We'll text you with updates. Goodbye.",
                "hangup": True,
            }

        contract = self.build_contract(session)
        return {
            "session_id": session_id,
            "say": contract.next_prompt,
            "prompt_id": contract.prompt_id,
        }

    async def handle_voice_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        call_sid = payload.get("CallSid")
        call_status = (payload.get("CallStatus") or payload.get("CallStatusCallbackEvent") or "").lower()
        session_id = self.repository.find_session_id_by_external_id(call_sid) if call_sid else None
        session = self.get_session_or_404(session_id) if session_id else None
        self.repository.save_event(
            TelephonyEvent(
                source="twilio_voice",
                event_type="voice_status",
                external_id=call_sid,
                session_id=session_id,
                payload=payload,
            )
        )
        created_action = None
        if session and call_status in {"no-answer", "busy", "failed", "canceled"}:
            logger.warning("Missed call: session=%s status=%s", session_id, call_status)
            created_action = self._create_missed_call_followup(
                session=session,
                reason=f"Missed call status: {call_status}",
                destination=session.ticket.customer.phone,
            )
        return {
            "ok": True,
            "session_id": session_id,
            "call_status": call_status,
            "followup_action": created_action.model_dump(mode="json") if created_action else None,
        }

    async def handle_sms_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        from_number = normalize_phone(payload.get("From"))
        body = (payload.get("Body") or "").strip()
        logger.info("SMS inbound from %s: %s", from_number or "unknown", body[:80])
        session = self.repository.find_latest_session_by_phone(from_number) if from_number else None
        if not session or session.submitted_to_openclaw:
            session = self.start_session(phone=from_number, channel="sms")
        message_sid = payload.get("MessageSid")
        self.repository.save_event(
            TelephonyEvent(
                source="twilio_sms",
                event_type="sms_inbound",
                external_id=message_sid,
                session_id=session.session_id,
                payload=payload,
            )
        )
        if body:
            session = await self.process_turn(session.session_id, body)

        if session.state == SessionState.SUBMITTED and not session.submitted_to_openclaw:
            logger.info("SMS auto-submit for session %s", session.session_id)
            session, _ = await self.submit_session(session.session_id)
            return {
                "session_id": session.session_id,
                "reply": "Your request has been submitted! We'll text you with updates shortly.",
                "prompt_id": "submitted_ready_01",
                "state": session.state.value,
                "allow_submit": False,
            }

        contract = self.build_contract(session)
        return {
            "session_id": session.session_id,
            "reply": contract.next_prompt,
            "prompt_id": contract.prompt_id,
            "state": session.state.value,
            "allow_submit": contract.allow_submit,
        }

    def _apply_candidate_updates(
        self,
        session: IntakeSession,
        candidates: CandidateFields,
        strict: bool = True,
    ) -> tuple[dict[str, str], dict[str, str]]:
        accepted: dict[str, str] = {}
        rejected: dict[str, str] = {}
        updates = candidates.as_update_dict()
        for field, value in updates.items():
            cleaned = self._validate_field(field, value)
            if cleaned is None:
                rejected[field] = f"Invalid value: {value!r}"
                continue
            if field == "customer_name":
                session.ticket.customer.name = cleaned
            elif field == "phone":
                session.ticket.customer.phone = cleaned
            elif field == "service":
                session.ticket.request.service = cleaned
            elif field == "preferred_time":
                session.ticket.request.preferred_time = cleaned
            elif field == "notes":
                session.ticket.request.notes = cleaned
            accepted[field] = cleaned
        if strict and not accepted and updates:
            session.last_assistant_message = "I couldn't safely apply those draft fields."
        return accepted, rejected

    def _validate_field(self, field: str, value: str) -> str | None:
        raw = (value or "").strip()
        if field == "customer_name":
            if 1 <= len(raw) <= 80 and re.fullmatch(r"[A-Za-z][A-Za-z\-\s']*", raw):
                return " ".join(part.capitalize() for part in raw.split())
            return None
        if field == "phone":
            return normalize_phone(raw)
        if field == "service":
            if 1 <= len(raw) <= 80:
                return raw.lower()
            return None
        if field == "preferred_time":
            if 1 <= len(raw) <= 80:
                return raw.lower()
            return None
        if field == "notes":
            if raw.lower() in {"no", "none", "nope", "nothing", "thats all", "that's all"}:
                return ""
            return sanitize_notes(raw)
        return None

    def _create_missed_call_followup(self, session: IntakeSession, reason: str, destination: str | None) -> FollowupAction:
        action = FollowupAction(
            session_id=session.session_id,
            ticket_id=session.ticket.ticket_id,
            action_type="sms_followup",
            channel="sms",
            destination=destination,
            reason=reason,
            payload={
                "suggested_message": "Sorry we missed your call. Reply here to continue your booking request.",
                "policy": "missed_call_recovery",
            },
        )
        self.repository.save_followup_action(action)
        session.transcript.append(ConversationMessage(role="system", content=f"Created follow-up action: {action.action_type} ({reason})"))
        session.touch()
        self.repository.save_session(session)
        return action

    async def execute_followup_action(self, action_id: str) -> dict[str, Any]:
        action = self.repository.get_followup_action(action_id)
        if not action:
            raise HTTPException(status_code=404, detail="Followup action not found")
        if action.status not in ("pending", "failed"):
            return {"action_id": action_id, "status": action.status, "skipped": True, "reason": "not pending or failed"}

        if action.action_type in ("sms_followup", "notify_owner") and action.destination:
            message = action.payload.get("suggested_message", "We have an update on your request.")
            result = await self._send_existing_followup(action, message)
            return result

        logger.warning("Followup %s: unsupported type=%s or no destination", action_id, action.action_type)
        return {"action_id": action_id, "status": "skipped", "reason": f"unsupported: {action.action_type}"}

    async def _send_existing_followup(self, action: FollowupAction, message: str) -> dict[str, Any]:
        from .sms_sender import send_sms

        result = await send_sms(action.destination, message, self.settings)
        new_status = "sent" if result.sent else "failed"
        self.repository.update_followup_action_status(action.action_id, new_status)
        logger.info("Followup %s executed: status=%s sms_sid=%s", action.action_id, new_status, result.sid)
        return {"action_id": action.action_id, "status": new_status, "sms_sid": result.sid, "error": result.error}

    async def execute_pending_followups(self) -> list[dict[str, Any]]:
        pending = self.repository.list_pending_followup_actions()
        logger.info("Executing %d pending/failed followup actions", len(pending))
        results = []
        for action in pending:
            result = await self.execute_followup_action(action.action_id)
            results.append(result)
        return results

    def _local_ticket_status_for_state(self, state: SessionState) -> str:
        if state == SessionState.SUBMITTED:
            return "confirmed"
        if state == SessionState.CONFIRMATION:
            return "ready_for_confirmation"
        return "draft"

    def _apply_llm_fields(self, session: IntakeSession, candidates: CandidateFields) -> None:
        updates = candidates.as_update_dict()
        for field, value in updates.items():
            if field == "phone":
                continue
            cleaned = self._validate_field(field, value)
            if cleaned is None:
                logger.debug("LLM field rejected: %s=%r", field, value)
                continue
            if field == "customer_name" and not session.ticket.customer.name:
                session.ticket.customer.name = cleaned
            elif field == "service" and not session.ticket.request.service:
                session.ticket.request.service = cleaned
            elif field == "preferred_time" and not session.ticket.request.preferred_time:
                session.ticket.request.preferred_time = cleaned
            elif field == "notes" and session.ticket.request.notes is None:
                session.ticket.request.notes = cleaned

    def _sanitize_llm_reply(self, reply: str, session: IntakeSession) -> str | None:
        if not reply or len(reply) < 5 or len(reply) > 500:
            logger.warning("LLM reply rejected: length=%d", len(reply) if reply else 0)
            return None
        lowered = reply.lower()
        bad_phrases = {"submitted", "confirmed your request", "ticket is complete", "request is complete", "all done"}
        for phrase in bad_phrases:
            if phrase in lowered:
                logger.warning("LLM reply rejected: contains '%s'", phrase)
                return None
        return reply


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        return f"+1{digits}"
    if 7 <= len(digits) <= 15:
        return f"+{digits}"
    return None