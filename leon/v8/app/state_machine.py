
from __future__ import annotations

from .extraction import apply_extractions
from .models import IntakeSession, PromptSpec, SessionState, Ticket

REQUIRED_FIELDS = ["customer_name", "phone", "service", "preferred_time", "notes"]

PROMPT_LIBRARY = {
    "intro_01": PromptSpec(
        prompt_id="intro_01",
        name="Opening greeting",
        text="Hi, thanks for calling. I can help start your request. May I have your name?",
        use_case="first-turn greeting",
        channel="phone",
    ),
    "ask_name_01": PromptSpec(
        prompt_id="ask_name_01",
        name="Ask for name",
        text="Thanks. May I have your name?",
        use_case="collect customer name",
    ),
    "ask_phone_01": PromptSpec(
        prompt_id="ask_phone_01",
        name="Ask for phone",
        text="What phone number should we use to text you updates?",
        use_case="collect customer phone",
    ),
    "ask_service_01": PromptSpec(
        prompt_id="ask_service_01",
        name="Ask for service",
        text="Thanks. What service do you need?",
        use_case="collect requested service",
    ),
    "ask_time_01": PromptSpec(
        prompt_id="ask_time_01",
        name="Ask for preferred time",
        text="What time works best for you?",
        use_case="collect preferred time",
    ),
    "ask_notes_01": PromptSpec(
        prompt_id="ask_notes_01",
        name="Ask for notes",
        text="Any notes you'd like me to add before I confirm? You can also say no.",
        use_case="collect notes",
    ),
    "confirm_01": PromptSpec(
        prompt_id="confirm_01",
        name="Confirmation",
        text="Let me confirm the request details.",
        use_case="read back collected form",
    ),
    "submitted_ready_01": PromptSpec(
        prompt_id="submitted_ready_01",
        name="Ready to submit",
        text="Perfect. This ticket is ready for backend submission.",
        use_case="ready for backend submit",
    ),
    "correction_01": PromptSpec(
        prompt_id="correction_01",
        name="Correction prompt",
        text="No problem. What would you like to change: name, phone, service, time, or notes?",
        use_case="correction loop",
    ),
    "repeat_01": PromptSpec(
        prompt_id="repeat_01",
        name="Repeat fallback",
        text="I didn't catch that. Could you say that one more time?",
        use_case="fallback for empty turn",
    ),
}


class IntakeStateMachine:
    def start_message(self) -> str:
        return PROMPT_LIBRARY["intro_01"].text

    def handle_turn(self, session: IntakeSession, user_input: str) -> str:
        text = user_input.strip()
        lowered = text.lower()
        if not text:
            return PROMPT_LIBRARY["repeat_01"].text

        current_state = session.state

        if current_state == SessionState.CONFIRMATION:
            return self._handle_confirmation(session, text)
        if current_state == SessionState.CORRECTION:
            return self._handle_correction(session, text)

        if current_state == SessionState.COLLECT_SERVICE and lowered in {"help", "?"}:
            return "You can say something like oil change, tire exchange, haircut, consultation, repair, or appointment. What service do you need?"
        if current_state == SessionState.COLLECT_TIME and lowered in {"help", "?"}:
            return "You can say something like tomorrow morning, Tuesday afternoon, or 10 am. What time works best for you?"

        collect_notes = current_state == SessionState.COLLECT_NOTES
        apply_extractions(session.ticket, text, current_state=current_state, collect_notes=collect_notes)

        next_state = self.resolve_state(session)
        session.state = next_state
        session.touch()
        return self.message_for_state(session)

    def refresh_after_ticket_change(self, session: IntakeSession) -> str:
        if session.submitted_to_openclaw:
            return session.last_assistant_message or "This ticket was already submitted."
        if session.state == SessionState.SUBMITTED:
            return PROMPT_LIBRARY["submitted_ready_01"].text
        session.state = self.resolve_state(session)
        session.touch()
        return self.message_for_state(session)

    def resolve_state(self, session: IntakeSession) -> SessionState:
        ticket = session.ticket
        if not ticket.customer.name:
            return SessionState.COLLECT_NAME
        if not ticket.customer.phone:
            return SessionState.COLLECT_PHONE
        if not ticket.request.service:
            return SessionState.COLLECT_SERVICE
        if not ticket.request.preferred_time:
            return SessionState.COLLECT_TIME
        if ticket.request.notes is None:
            return SessionState.COLLECT_NOTES
        if session.state == SessionState.SUBMITTED:
            return SessionState.SUBMITTED
        return SessionState.CONFIRMATION

    def message_for_state(self, session: IntakeSession) -> str:
        ticket = session.ticket
        state = session.state
        if state == SessionState.COLLECT_NAME:
            return PROMPT_LIBRARY["ask_name_01"].text
        if state == SessionState.COLLECT_PHONE:
            return PROMPT_LIBRARY["ask_phone_01"].text
        if state == SessionState.COLLECT_SERVICE:
            return f"Thanks{_name_suffix(ticket.customer.name)}. What service do you need?"
        if state == SessionState.COLLECT_TIME:
            return PROMPT_LIBRARY["ask_time_01"].text
        if state == SessionState.COLLECT_NOTES:
            return PROMPT_LIBRARY["ask_notes_01"].text
        if state == SessionState.CONFIRMATION:
            summary = self._build_summary(ticket)
            return f"Let me confirm: {summary}. Is that correct?"
        if state == SessionState.SUBMITTED:
            return PROMPT_LIBRARY["submitted_ready_01"].text
        return self.start_message()

    def missing_fields(self, ticket: Ticket) -> list[str]:
        missing: list[str] = []
        if not ticket.customer.name:
            missing.append("customer_name")
        if not ticket.customer.phone:
            missing.append("phone")
        if not ticket.request.service:
            missing.append("service")
        if not ticket.request.preferred_time:
            missing.append("preferred_time")
        if ticket.request.notes is None:
            missing.append("notes")
        return missing

    def current_field(self, session: IntakeSession) -> str | None:
        state = self.resolve_state(session)
        mapping = {
            SessionState.COLLECT_NAME: "customer_name",
            SessionState.COLLECT_PHONE: "phone",
            SessionState.COLLECT_SERVICE: "service",
            SessionState.COLLECT_TIME: "preferred_time",
            SessionState.COLLECT_NOTES: "notes",
        }
        return mapping.get(state)

    def prompt_id_for_state(self, state: SessionState) -> str:
        mapping = {
            SessionState.COLLECT_NAME: "ask_name_01",
            SessionState.COLLECT_PHONE: "ask_phone_01",
            SessionState.COLLECT_SERVICE: "ask_service_01",
            SessionState.COLLECT_TIME: "ask_time_01",
            SessionState.COLLECT_NOTES: "ask_notes_01",
            SessionState.CONFIRMATION: "confirm_01",
            SessionState.CORRECTION: "correction_01",
            SessionState.SUBMITTED: "submitted_ready_01",
        }
        return mapping.get(state, "intro_01")

    def play_audio_id_for_session(self, session: IntakeSession) -> str | None:
        if session.ticket.channel != "phone":
            return None
        return self.prompt_id_for_state(self.resolve_state(session))

    def required_fields(self) -> list[str]:
        return REQUIRED_FIELDS.copy()

    def confirmation_summary(self, ticket: Ticket) -> str:
        return self._build_summary(ticket)

    def suggested_next_question(self, session: IntakeSession) -> str:
        state = self.resolve_state(session)
        temp_session = session.model_copy(deep=True)
        temp_session.state = state
        return self.message_for_state(temp_session)

    def _handle_confirmation(self, session: IntakeSession, text: str) -> str:
        lowered = text.lower()
        if lowered in {"yes", "y", "correct", "that's correct", "thats correct", "looks good"}:
            session.state = SessionState.SUBMITTED
            session.ticket.status = "confirmed"
            session.touch()
            return "Perfect. I have your request and I'm submitting it now."

        if lowered in {"no", "n", "not correct", "change it"}:
            session.state = SessionState.CORRECTION
            session.touch()
            return PROMPT_LIBRARY["correction_01"].text

        return "Please say yes if the details are correct, or no if you'd like to change something."

    def _handle_correction(self, session: IntakeSession, text: str) -> str:
        lowered = text.lower()
        ticket = session.ticket

        if "name" in lowered:
            ticket.customer.name = None
            session.state = SessionState.COLLECT_NAME
            session.touch()
            return "Okay, let's update your name. What name should I use?"
        if "phone" in lowered or "number" in lowered:
            ticket.customer.phone = None
            session.state = SessionState.COLLECT_PHONE
            session.touch()
            return "Okay, what phone number should we use?"
        if "service" in lowered:
            ticket.request.service = None
            session.state = SessionState.COLLECT_SERVICE
            session.touch()
            return "Okay, what service do you need?"
        if "time" in lowered or "date" in lowered:
            ticket.request.preferred_time = None
            session.state = SessionState.COLLECT_TIME
            session.touch()
            return "Okay, what time works best for you?"
        if "note" in lowered:
            ticket.request.notes = None
            session.state = SessionState.COLLECT_NOTES
            session.touch()
            return "Okay, what notes would you like me to add?"

        apply_extractions(ticket, text, current_state=SessionState.CORRECTION, collect_notes=True)
        session.state = self.resolve_state(session)
        session.touch()
        return self.message_for_state(session)

    def _build_summary(self, ticket: Ticket) -> str:
        parts = [
            f"name {ticket.customer.name}",
            f"phone {ticket.customer.phone}",
            f"service {ticket.request.service}",
            f"preferred time {ticket.request.preferred_time}",
        ]
        if ticket.request.notes:
            parts.append(f"notes {ticket.request.notes}")
        return ", ".join(parts)


def _name_suffix(name: str | None) -> str:
    return f", {name}" if name else ""
