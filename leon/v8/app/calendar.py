"""Calendar/booking layer — parse times, check availability, create bookings."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from .database import SessionRepository
from .models import Booking, IntakeSession

logger = logging.getLogger("intake.calendar")

# Max bookings per day (simple capacity limit)
DEFAULT_MAX_BOOKINGS_PER_DAY = 10


def parse_booking_date(preferred_time: str | None) -> str | None:
    """Try to parse a preferred_time string into a YYYY-MM-DD date.

    Handles: 'today', 'tomorrow', 'monday'-'sunday', 'next week',
    and date-like strings.
    """
    if not preferred_time:
        return None

    lowered = preferred_time.lower().strip()
    today = date.today()

    if "today" in lowered:
        return today.isoformat()
    if "tomorrow" in lowered:
        return (today + timedelta(days=1)).isoformat()
    if "next week" in lowered:
        # Next Monday
        days_ahead = 7 - today.weekday()
        return (today + timedelta(days=days_ahead)).isoformat()

    # Day of week
    days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    for day_name, day_num in days.items():
        if day_name in lowered:
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # Next occurrence, not today
            return (today + timedelta(days=days_ahead)).isoformat()

    # Try to parse explicit date
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(lowered, fmt)
            if parsed.year == 1900:  # No year specified
                parsed = parsed.replace(year=today.year)
            return parsed.date().isoformat()
        except ValueError:
            continue

    # Fallback: use tomorrow if we can't parse
    logger.debug("Could not parse date from '%s', defaulting to tomorrow", preferred_time)
    return (today + timedelta(days=1)).isoformat()


def parse_booking_time(preferred_time: str | None) -> str | None:
    """Extract a time-of-day from preferred_time string."""
    if not preferred_time:
        return None

    lowered = preferred_time.lower().strip()

    # Explicit time like "2 pm", "10:30 am"
    time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', lowered)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        ampm = time_match.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"

    # Descriptive times
    if "morning" in lowered:
        return "09:00"
    if "afternoon" in lowered:
        return "14:00"
    if "evening" in lowered:
        return "17:00"

    return None


def check_availability(
    repository: SessionRepository,
    booking_date: str,
    max_per_day: int = DEFAULT_MAX_BOOKINGS_PER_DAY,
) -> dict:
    """Check if a date has availability."""
    count = repository.count_bookings_for_date(booking_date)
    available = count < max_per_day
    return {
        "date": booking_date,
        "bookings": count,
        "max": max_per_day,
        "available": available,
        "remaining_slots": max(0, max_per_day - count),
    }


def create_booking_from_session(
    session: IntakeSession,
    repository: SessionRepository,
) -> Booking | None:
    """Create a booking from a submitted session's ticket."""
    ticket = session.ticket
    booking_date = parse_booking_date(ticket.request.preferred_time)
    booking_time = parse_booking_time(ticket.request.preferred_time)

    if not booking_date:
        logger.warning("Could not parse booking date from: %s", ticket.request.preferred_time)
        return None

    booking = Booking(
        ticket_id=ticket.ticket_id,
        session_id=session.session_id,
        customer_name=ticket.customer.name,
        customer_phone=ticket.customer.phone,
        service=ticket.request.service or "unknown",
        booking_date=booking_date,
        booking_time=booking_time,
        notes=ticket.request.notes,
    )
    repository.save_booking(booking)
    logger.info("Booking created: %s date=%s time=%s service=%s", booking.booking_id, booking_date, booking_time, booking.service)
    return booking
