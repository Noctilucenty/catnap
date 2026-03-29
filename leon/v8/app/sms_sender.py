from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import Settings

logger = logging.getLogger("intake.sms")

TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


@dataclass
class SmsResult:
    sent: bool
    sid: str | None = None
    error: str | None = None


async def send_sms(to: str, body: str, settings: Settings) -> SmsResult:
    if not settings.twilio_enabled:
        logger.info("SMS skipped (Twilio disabled): to=%s body=%s", to, body[:60])
        return SmsResult(sent=False, error="twilio_disabled")

    if not all([settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number]):
        logger.warning("SMS skipped: Twilio credentials not configured")
        return SmsResult(sent=False, error="twilio_not_configured")

    url = TWILIO_MESSAGES_URL.format(sid=settings.twilio_account_sid)
    logger.info("Sending SMS: to=%s from=%s body=%s", to, settings.twilio_from_number, body[:60])

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                data={
                    "To": to,
                    "From": settings.twilio_from_number,
                    "Body": body,
                },
            )
            response.raise_for_status()
            data = response.json()
            message_sid = data.get("sid")
            logger.info("SMS sent: sid=%s to=%s", message_sid, to)
            return SmsResult(sent=True, sid=message_sid)
    except httpx.HTTPStatusError as exc:
        logger.error("SMS HTTP error: status=%s body=%s", exc.response.status_code, exc.response.text[:200])
        return SmsResult(sent=False, error=f"http_{exc.response.status_code}")
    except httpx.RequestError as exc:
        logger.error("SMS request failed: %s", exc)
        return SmsResult(sent=False, error=str(exc))
