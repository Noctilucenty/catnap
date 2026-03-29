
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class SessionState(str, Enum):
    GREETING = "greeting"
    COLLECT_NAME = "collect_name"
    COLLECT_PHONE = "collect_phone"
    COLLECT_SERVICE = "collect_service"
    COLLECT_TIME = "collect_time"
    COLLECT_NOTES = "collect_notes"
    CONFIRMATION = "confirmation"
    CORRECTION = "correction"
    SUBMITTED = "submitted"


class ConversationMessage(BaseModel):
    role: str
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Customer(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None


class RequestDetails(BaseModel):
    intent: str = "appointment_request"
    service: Optional[str] = None
    preferred_time: Optional[str] = None
    notes: Optional[str] = None


class Ticket(BaseModel):
    ticket_id: str = Field(default_factory=lambda: f"tk_{uuid4().hex[:10]}")
    channel: str = "phone"
    customer: Customer = Field(default_factory=Customer)
    request: RequestDetails = Field(default_factory=RequestDetails)
    status: str = "draft"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class IntakeSession(BaseModel):
    session_id: str = Field(default_factory=lambda: f"sess_{uuid4().hex[:10]}")
    state: SessionState = SessionState.GREETING
    ticket: Ticket = Field(default_factory=Ticket)
    last_assistant_message: Optional[str] = None
    submitted_to_openclaw: bool = False
    openclaw_response: Optional[dict] = None
    transcript: list[ConversationMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)
        self.ticket.touch()


class CandidateFields(BaseModel):
    customer_name: Optional[str] = None
    phone: Optional[str] = None
    service: Optional[str] = None
    preferred_time: Optional[str] = None
    notes: Optional[str] = None

    def as_update_dict(self) -> dict[str, str]:
        updates: dict[str, str] = {}
        if self.customer_name is not None:
            updates["customer_name"] = self.customer_name
        if self.phone is not None:
            updates["phone"] = self.phone
        if self.service is not None:
            updates["service"] = self.service
        if self.preferred_time is not None:
            updates["preferred_time"] = self.preferred_time
        if self.notes is not None:
            updates["notes"] = self.notes
        return updates


class PromptSpec(BaseModel):
    prompt_id: str
    name: str
    text: str
    use_case: str
    channel: str = "any"


class ConversationContract(BaseModel):
    session_id: str
    state: SessionState
    channel: str
    current_field: Optional[str] = None
    required_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    next_prompt: str
    prompt_id: str
    play_audio_id: Optional[str] = None
    allow_submit: bool = False
    needs_confirmation: bool = False
    draft_form: CandidateFields = Field(default_factory=CandidateFields)
    authoritative_ticket: Ticket
    confirmation_summary: Optional[str] = None
    suggested_channel_switch: Optional[str] = None


class StartSessionRequest(BaseModel):
    phone: Optional[str] = None
    channel: str = "phone"


class StartSessionResponse(BaseModel):
    session_id: str
    state: SessionState
    assistant_message: str
    ticket: Ticket
    transcript: list[ConversationMessage] = Field(default_factory=list)
    contract: ConversationContract


class UserTurnRequest(BaseModel):
    user_input: str


class UserTurnResponse(BaseModel):
    session_id: str
    state: SessionState
    assistant_message: str
    ticket: Ticket
    submitted_to_openclaw: bool = False
    openclaw_response: Optional[dict] = None
    transcript: list[ConversationMessage] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    contract: ConversationContract


class SubmitSessionResponse(BaseModel):
    session_id: str
    state: SessionState
    ticket: Ticket
    submitted_to_openclaw: bool
    openclaw_response: Optional[dict] = None
    message: str
    transcript: list[ConversationMessage] = Field(default_factory=list)
    contract: ConversationContract


class SuggestFromTextRequest(BaseModel):
    text: str


class SuggestFromTextResponse(BaseModel):
    session_id: str
    source_text: str
    candidate_fields: CandidateFields
    missing_fields: list[str] = Field(default_factory=list)
    suggested_next_question: str
    preview_ticket: Ticket


class ProposeTicketRequest(CandidateFields):
    source: str = "frontend_ai"
    raw_text: Optional[str] = None
    auto_advance: bool = True


class ProposeTicketResponse(BaseModel):
    session_id: str
    state: SessionState
    ticket: Ticket
    accepted_updates: dict[str, str] = Field(default_factory=dict)
    rejected_updates: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    suggested_next_question: str
    source: str
    contract: ConversationContract


class TicketSummary(BaseModel):
    ticket_id: str
    session_id: str
    status: str
    customer_name: Optional[str] = None
    service: Optional[str] = None
    preferred_time: Optional[str] = None
    created_at: datetime | str
    updated_at: datetime | str
    submitted_to_openclaw: bool = False


class TicketDetail(BaseModel):
    session_id: str
    ticket: Ticket
    submitted_to_openclaw: bool = False
    openclaw_response: Optional[dict] = None


class FollowupAction(BaseModel):
    action_id: str = Field(default_factory=lambda: f"act_{uuid4().hex[:10]}")
    session_id: str
    ticket_id: str
    action_type: str
    channel: str
    destination: Optional[str] = None
    status: str = "pending"
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class TelephonyEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid4().hex[:10]}")
    source: str
    event_type: str
    external_id: Optional[str] = None
    session_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FollowupActionSummary(BaseModel):
    action_id: str
    session_id: str
    ticket_id: str
    action_type: str
    channel: str
    destination: Optional[str] = None
    status: str
    reason: str
    created_at: datetime | str
    updated_at: datetime | str


class HealthResponse(BaseModel):
    status: str
    app_name: str
    db_path: str


class OpenClawSubmissionResult(BaseModel):
    submitted: bool
    payload: dict
    response: Optional[dict] = None


class ReviewItem(BaseModel):
    review_id: str = Field(default_factory=lambda: f"rev_{uuid4().hex[:10]}")
    ticket_id: str
    session_id: str
    reason: str
    status: str = "pending"
    openclaw_summary: Optional[str] = None
    openclaw_next_step: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReviewItemSummary(BaseModel):
    review_id: str
    ticket_id: str
    session_id: str
    customer_name: Optional[str] = None
    service: Optional[str] = None
    reason: str
    status: str
    openclaw_summary: Optional[str] = None
    created_at: datetime | str
    updated_at: datetime | str


class ResolveReviewRequest(BaseModel):
    resolved_by: str = "owner"
    resolution_notes: Optional[str] = None


class Booking(BaseModel):
    booking_id: str = Field(default_factory=lambda: f"bk_{uuid4().hex[:10]}")
    ticket_id: str
    session_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    service: str
    booking_date: str  # YYYY-MM-DD
    booking_time: Optional[str] = None  # HH:MM or descriptive
    duration_minutes: int = 60
    status: str = "confirmed"
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BookingSummary(BaseModel):
    booking_id: str
    ticket_id: str
    customer_name: Optional[str] = None
    service: str
    booking_date: str
    booking_time: Optional[str] = None
    status: str
    created_at: datetime | str
