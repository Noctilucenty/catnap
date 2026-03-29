from __future__ import annotations

import re
from typing import Optional

from .models import CandidateFields, SessionState, Ticket

SERVICE_SYNONYMS = {
    "booking": "appointment",
    "book": "appointment",
    "appointment": "appointment",
    "schedule": "appointment",
    "reservation": "appointment",
    "tire exchanging": "tire exchange",
    "tyre exchange": "tire exchange",
}

SERVICE_KEYWORDS = [
    "oil change",
    "tire rotation",
    "tire exchange",
    "brake inspection",
    "haircut",
    "consultation",
    "cleaning",
    "repair",
    "appointment",
    "estimate",
]

TIME_PATTERNS = [
    r"\btomorrow(?: morning| afternoon| evening)?\b",
    r"\btoday(?: morning| afternoon| evening)?\b",
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?: morning| afternoon| evening)?\b",
    r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b",
    r"\bmorning\b",
    r"\bafternoon\b",
    r"\bevening\b",
    r"\bnext week\b",
]

NAME_PATTERNS = [
    r"(?:my name is|this is|i am|i'm)\s+([A-Za-z][A-Za-z\-']+(?:\s+[A-Za-z][A-Za-z\-']+)?)",
]

PHONE_PATTERN = re.compile(r"(?:\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})")
NON_SERVICE_WORDS = {"help", "idk", "unknown", "not sure", "maybe"}
NEGATIVE_NOTES = {"no", "none", "nope", "nothing", "that's all", "thats all"}
PROMPT_INJECTION_MARKERS = {"ignore previous", "system prompt", "developer message", "act as"}


def extract_name(text: str) -> Optional[str]:
    for pattern in NAME_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _title_clean(match.group(1))

    cleaned = text.strip()
    if 1 <= len(cleaned.split()) <= 3 and cleaned.replace(" ", "").replace("-", "").replace("'", "").isalpha():
        lowered = cleaned.lower().strip()
        if lowered not in NON_SERVICE_WORDS and lowered not in SERVICE_SYNONYMS:
            return _title_clean(cleaned)
    return None


def extract_service(text: str) -> Optional[str]:
    lowered = text.lower().strip(" .,!?")

    if lowered in NON_SERVICE_WORDS:
        return None

    if lowered in SERVICE_SYNONYMS:
        return SERVICE_SYNONYMS[lowered]

    for keyword in SERVICE_KEYWORDS:
        if keyword in lowered:
            return keyword

    service_match = re.search(
        r"(?:need|want|book|schedule|get)\s+(?:an?\s+)?(.+?)(?:\s+(?:for|on|at|tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|$)",
        lowered,
    )
    if service_match:
        candidate = service_match.group(1).strip(" .,!?")
        if candidate in SERVICE_SYNONYMS:
            return SERVICE_SYNONYMS[candidate]
        if 1 <= len(candidate.split()) <= 6 and candidate not in NON_SERVICE_WORDS:
            return candidate

    if 1 <= len(lowered.split()) <= 4 and lowered not in NON_SERVICE_WORDS:
        if re.fullmatch(r"[a-z][a-z\s\-]{1,30}", lowered):
            return lowered
    return None


def extract_preferred_time(text: str) -> Optional[str]:
    lowered = text.lower()
    for pattern in TIME_PATTERNS:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()

    time_match = re.search(r"(?:for|on|at)\s+(.+)$", lowered)
    if time_match:
        candidate = time_match.group(1).strip(" .,!?")
        if 1 <= len(candidate.split()) <= 6:
            return candidate
    return None


def extract_phone(text: str) -> Optional[str]:
    match = PHONE_PATTERN.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    if len(digits) == 10:
        return f"+1{digits}"
    return None


def extract_notes(text: str) -> Optional[str]:
    lowered = text.lower().strip()
    if lowered in NEGATIVE_NOTES:
        return ""
    if "manager" in lowered:
        return "customer wants to speak with manager"
    note_match = re.search(r"(?:note|notes|add)[:\s]+(.+)$", lowered)
    if note_match:
        return sanitize_notes(note_match.group(1).strip())
    return None


def sanitize_notes(text: str) -> str:
    cleaned = text.strip()
    lowered = cleaned.lower()
    for marker in PROMPT_INJECTION_MARKERS:
        lowered = lowered.replace(marker, "[filtered]")
    return lowered[:500]


def extract_candidate_fields(text: str) -> CandidateFields:
    text = text.strip()
    return CandidateFields(
        customer_name=extract_name(text),
        phone=extract_phone(text),
        service=extract_service(text),
        preferred_time=extract_preferred_time(text),
        notes=extract_notes(text),
    )


def apply_extractions(ticket: Ticket, text: str, current_state: SessionState, collect_notes: bool = False) -> None:
    phone = extract_phone(text)
    if phone and not ticket.customer.phone:
        ticket.customer.phone = phone

    if current_state == SessionState.COLLECT_NAME:
        name = extract_name(text)
        if name and not ticket.customer.name:
            ticket.customer.name = name
        service = extract_service(text)
        preferred_time = extract_preferred_time(text)
        if service and any(token in text.lower() for token in ["need", "want", "book", "schedule"]):
            ticket.request.service = ticket.request.service or service
        if preferred_time and any(token in text.lower() for token in ["today", "tomorrow", "am", "pm", "morning", "afternoon", "evening"]):
            ticket.request.preferred_time = ticket.request.preferred_time or preferred_time
        return

    if current_state == SessionState.COLLECT_PHONE:
        if phone and not ticket.customer.phone:
            ticket.customer.phone = phone
        return

    if current_state == SessionState.COLLECT_SERVICE:
        service = extract_service(text)
        if service and not ticket.request.service:
            ticket.request.service = service
        preferred_time = extract_preferred_time(text)
        if preferred_time and not ticket.request.preferred_time:
            ticket.request.preferred_time = preferred_time
        return

    if current_state == SessionState.COLLECT_TIME:
        preferred_time = extract_preferred_time(text)
        if preferred_time and not ticket.request.preferred_time:
            ticket.request.preferred_time = preferred_time
        service = extract_service(text)
        if service and not ticket.request.service and any(token in text.lower() for token in ["need", "want", "book", "schedule"]):
            ticket.request.service = service
        return

    if current_state == SessionState.COLLECT_NOTES or collect_notes:
        stripped = text.strip()
        if stripped and stripped.lower() not in NEGATIVE_NOTES:
            ticket.request.notes = sanitize_notes(stripped)
        elif stripped.lower() in NEGATIVE_NOTES:
            ticket.request.notes = ""
        return

    if current_state == SessionState.CORRECTION:
        name = extract_name(text)
        service = extract_service(text)
        preferred_time = extract_preferred_time(text)
        if name and not ticket.customer.name:
            ticket.customer.name = name
        if phone and not ticket.customer.phone:
            ticket.customer.phone = phone
        if service and not ticket.request.service:
            ticket.request.service = service
        if preferred_time and not ticket.request.preferred_time:
            ticket.request.preferred_time = preferred_time


def _title_clean(value: str) -> str:
    return " ".join(part.capitalize() for part in value.strip().split())
