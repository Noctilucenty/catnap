from __future__ import annotations

import asyncio
import json

from .config import get_settings
from .database import SessionRepository, get_connection, init_db
from .models import IntakeSession, SessionState
from .service import IntakeService

HELP_TEXT = """Commands:
  /show    Show current ticket JSON
  /state   Show current state
  /submit  Submit if the session is ready
  /restart Start a new session
  /quit    Exit
"""


def build_service() -> IntakeService:
    settings = get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)
    repo = SessionRepository(conn)
    return IntakeService(repo, settings)


async def run_cli() -> None:
    service = build_service()
    print("Intake Service CLI v0.4")
    print("Type /quit to exit. Type /show to inspect the current ticket.")
    print()

    phone = input("Optional customer phone (press Enter to skip): ").strip() or None
    session = service.start_session(phone=phone)
    print(f"\nAI: {session.last_assistant_message}")

    while True:
        user_input = input("You: ").strip()

        if not user_input:
            print("AI: I didn't catch that. Could you say that one more time?")
            continue

        if user_input == "/quit":
            print("Exiting.")
            return
        if user_input in {"/help", "help"}:
            print(HELP_TEXT)
            continue
        if user_input == "/show":
            print(json.dumps(session.ticket.model_dump(mode="json"), indent=2))
            continue
        if user_input == "/state":
            print(f"Current state: {session.state.value}")
            continue
        if user_input == "/restart":
            session = service.start_session(phone=phone)
            print(f"\nAI: {session.last_assistant_message}")
            continue
        if user_input == "/submit":
            try:
                session, status_text = await service.submit_session(session.session_id)
                print(f"[submission] {status_text}")
                print("AI: Thanks. Your request has been submitted. We'll follow up with the next step shortly.")
            except Exception as exc:  # noqa: BLE001
                print(f"[submission] {exc}")
            continue

        session = await service.process_turn(session.session_id, user_input)
        print(f"AI: {session.last_assistant_message}")

        if session.state == SessionState.SUBMITTED:
            print("[ready] Session reached submitted state. Type /submit to forward the confirmed ticket.")
            print("[hint] You can use /show to inspect the final ticket before sending it.")


if __name__ == "__main__":
    asyncio.run(run_cli())
