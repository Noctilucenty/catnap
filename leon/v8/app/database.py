from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .models import Booking, BookingSummary, FollowupAction, FollowupActionSummary, IntakeSession, ReviewItem, ReviewItemSummary, TelephonyEvent, TicketDetail, TicketSummary

logger = logging.getLogger("intake.database")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def get_connection(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL,
            customer_name TEXT,
            customer_phone TEXT,
            service TEXT,
            preferred_time TEXT,
            submitted_to_openclaw INTEGER NOT NULL DEFAULT 0,
            openclaw_response_json TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS followup_actions (
            action_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            channel TEXT NOT NULL,
            destination TEXT,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS communication_events (
            event_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            external_id TEXT,
            session_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_queue (
            review_id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            openclaw_summary TEXT,
            openclaw_next_step TEXT,
            resolved_by TEXT,
            resolution_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            booking_id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            customer_name TEXT,
            customer_phone TEXT,
            service TEXT NOT NULL,
            booking_date TEXT NOT NULL,
            booking_time TEXT,
            duration_minutes INTEGER DEFAULT 60,
            status TEXT NOT NULL DEFAULT 'confirmed',
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(conn, "tickets", "customer_phone", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_session_id ON tickets(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_updated_at ON tickets(updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_phone ON tickets(customer_phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_followup_updated_at ON followup_actions(updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_external_id ON communication_events(external_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session_id ON communication_events(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(booking_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)")
    conn.commit()


class SessionRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save_session(self, session: IntakeSession) -> None:
        session_json = json.dumps(session.model_dump(mode="json"))
        ticket = session.ticket
        ticket_json = json.dumps(ticket.model_dump(mode="json"))
        self.conn.execute(
            """
            INSERT INTO sessions (session_id, state, ticket_id, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                state=excluded.state,
                ticket_id=excluded.ticket_id,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                session.session_id,
                session.state.value,
                ticket.ticket_id,
                session_json,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO tickets (
                ticket_id, session_id, status, customer_name, customer_phone, service, preferred_time,
                submitted_to_openclaw, openclaw_response_json, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket_id) DO UPDATE SET
                session_id=excluded.session_id,
                status=excluded.status,
                customer_name=excluded.customer_name,
                customer_phone=excluded.customer_phone,
                service=excluded.service,
                preferred_time=excluded.preferred_time,
                submitted_to_openclaw=excluded.submitted_to_openclaw,
                openclaw_response_json=excluded.openclaw_response_json,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                ticket.ticket_id,
                session.session_id,
                ticket.status,
                ticket.customer.name,
                ticket.customer.phone,
                ticket.request.service,
                ticket.request.preferred_time,
                1 if session.submitted_to_openclaw else 0,
                json.dumps(session.openclaw_response) if session.openclaw_response is not None else None,
                ticket_json,
                ticket.created_at.isoformat(),
                ticket.updated_at.isoformat(),
            ),
        )
        self.conn.commit()

    def get_session(self, session_id: str) -> Optional[IntakeSession]:
        row = self.conn.execute(
            "SELECT payload_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return IntakeSession.model_validate(json.loads(row["payload_json"]))

    def find_latest_session_by_phone(self, phone: str) -> Optional[IntakeSession]:
        row = self.conn.execute(
            """
            SELECT s.payload_json
            FROM sessions s
            JOIN tickets t ON t.session_id = s.session_id
            WHERE t.customer_phone = ?
              AND s.updated_at >= datetime('now', '-24 hours')
            ORDER BY s.updated_at DESC
            LIMIT 1
            """,
            (phone,),
        ).fetchone()
        if not row:
            return None
        return IntakeSession.model_validate(json.loads(row["payload_json"]))

    def list_tickets(self, limit: int = 50) -> list[TicketSummary]:
        rows = self.conn.execute(
            """
            SELECT ticket_id, session_id, status, customer_name, service, preferred_time,
                   submitted_to_openclaw, created_at, updated_at
            FROM tickets
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            TicketSummary(
                ticket_id=row["ticket_id"],
                session_id=row["session_id"],
                status=row["status"],
                customer_name=row["customer_name"],
                service=row["service"],
                preferred_time=row["preferred_time"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                submitted_to_openclaw=bool(row["submitted_to_openclaw"]),
            )
            for row in rows
        ]

    def get_ticket_detail(self, ticket_id: str) -> Optional[TicketDetail]:
        row = self.conn.execute(
            """
            SELECT session_id, payload_json, submitted_to_openclaw, openclaw_response_json
            FROM tickets
            WHERE ticket_id = ?
            """,
            (ticket_id,),
        ).fetchone()
        if not row:
            return None
        response = json.loads(row["openclaw_response_json"]) if row["openclaw_response_json"] else None
        from .models import Ticket

        return TicketDetail(
            session_id=row["session_id"],
            ticket=Ticket.model_validate(json.loads(row["payload_json"])),
            submitted_to_openclaw=bool(row["submitted_to_openclaw"]),
            openclaw_response=response,
        )

    def save_followup_action(self, action: FollowupAction) -> None:
        self.conn.execute(
            """
            INSERT INTO followup_actions (
                action_id, session_id, ticket_id, action_type, channel, destination,
                status, reason, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(action_id) DO UPDATE SET
                status=excluded.status,
                destination=excluded.destination,
                reason=excluded.reason,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                action.action_id,
                action.session_id,
                action.ticket_id,
                action.action_type,
                action.channel,
                action.destination,
                action.status,
                action.reason,
                json.dumps(action.payload),
                action.created_at.isoformat(),
                action.updated_at.isoformat(),
            ),
        )
        self.conn.commit()

    def list_pending_followup_actions(self, limit: int = 50) -> list[FollowupAction]:
        """Return followup actions with status 'pending' or 'failed' (for retry)."""
        rows = self.conn.execute(
            """
            SELECT action_id, session_id, ticket_id, action_type, channel, destination,
                   status, reason, payload_json, created_at, updated_at
            FROM followup_actions
            WHERE status IN ('pending', 'failed')
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            FollowupAction(
                action_id=row["action_id"],
                session_id=row["session_id"],
                ticket_id=row["ticket_id"],
                action_type=row["action_type"],
                channel=row["channel"],
                destination=row["destination"],
                status=row["status"],
                reason=row["reason"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def get_followup_action(self, action_id: str) -> Optional[FollowupAction]:
        row = self.conn.execute(
            """
            SELECT action_id, session_id, ticket_id, action_type, channel, destination,
                   status, reason, payload_json, created_at, updated_at
            FROM followup_actions
            WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if not row:
            return None
        return FollowupAction(
            action_id=row["action_id"],
            session_id=row["session_id"],
            ticket_id=row["ticket_id"],
            action_type=row["action_type"],
            channel=row["channel"],
            destination=row["destination"],
            status=row["status"],
            reason=row["reason"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_followup_action_status(self, action_id: str, status: str) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE followup_actions SET status = ?, updated_at = ? WHERE action_id = ?",
            (status, now, action_id),
        )
        self.conn.commit()

    def list_followup_actions(self, limit: int = 50) -> list[FollowupActionSummary]:
        rows = self.conn.execute(
            """
            SELECT action_id, session_id, ticket_id, action_type, channel, destination,
                   status, reason, created_at, updated_at
            FROM followup_actions
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            FollowupActionSummary(
                action_id=row["action_id"],
                session_id=row["session_id"],
                ticket_id=row["ticket_id"],
                action_type=row["action_type"],
                channel=row["channel"],
                destination=row["destination"],
                status=row["status"],
                reason=row["reason"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def save_event(self, event: TelephonyEvent) -> None:
        self.conn.execute(
            """
            INSERT INTO communication_events (event_id, source, event_type, external_id, session_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                event.event_id,
                event.source,
                event.event_type,
                event.external_id,
                event.session_id,
                json.dumps(event.payload),
                event.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def find_session_id_by_external_id(self, external_id: str) -> Optional[str]:
        row = self.conn.execute(
            """
            SELECT session_id
            FROM communication_events
            WHERE external_id = ? AND session_id IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (external_id,),
        ).fetchone()
        return row["session_id"] if row else None

    # ── Review queue ──

    def save_review_item(self, item: ReviewItem) -> None:
        self.conn.execute(
            """
            INSERT INTO review_queue (
                review_id, ticket_id, session_id, reason, status,
                openclaw_summary, openclaw_next_step,
                resolved_by, resolution_notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(review_id) DO UPDATE SET
                status=excluded.status,
                resolved_by=excluded.resolved_by,
                resolution_notes=excluded.resolution_notes,
                updated_at=excluded.updated_at
            """,
            (
                item.review_id,
                item.ticket_id,
                item.session_id,
                item.reason,
                item.status,
                item.openclaw_summary,
                item.openclaw_next_step,
                item.resolved_by,
                item.resolution_notes,
                item.created_at.isoformat() if hasattr(item.created_at, "isoformat") else item.created_at,
                item.updated_at.isoformat() if hasattr(item.updated_at, "isoformat") else item.updated_at,
            ),
        )
        self.conn.commit()

    def list_reviews(self, status: str | None = None, limit: int = 50) -> list[ReviewItemSummary]:
        if status:
            rows = self.conn.execute(
                """
                SELECT r.review_id, r.ticket_id, r.session_id, r.reason, r.status,
                       r.openclaw_summary, r.created_at, r.updated_at,
                       t.customer_name, t.service
                FROM review_queue r
                LEFT JOIN tickets t ON t.ticket_id = r.ticket_id
                WHERE r.status = ?
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT r.review_id, r.ticket_id, r.session_id, r.reason, r.status,
                       r.openclaw_summary, r.created_at, r.updated_at,
                       t.customer_name, t.service
                FROM review_queue r
                LEFT JOIN tickets t ON t.ticket_id = r.ticket_id
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ReviewItemSummary(
                review_id=row["review_id"],
                ticket_id=row["ticket_id"],
                session_id=row["session_id"],
                customer_name=row["customer_name"],
                service=row["service"],
                reason=row["reason"],
                status=row["status"],
                openclaw_summary=row["openclaw_summary"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def get_review_item(self, review_id: str) -> Optional[ReviewItem]:
        row = self.conn.execute(
            "SELECT * FROM review_queue WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if not row:
            return None
        return ReviewItem(
            review_id=row["review_id"],
            ticket_id=row["ticket_id"],
            session_id=row["session_id"],
            reason=row["reason"],
            status=row["status"],
            openclaw_summary=row["openclaw_summary"],
            openclaw_next_step=row["openclaw_next_step"],
            resolved_by=row["resolved_by"],
            resolution_notes=row["resolution_notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def resolve_review(self, review_id: str, resolved_by: str, notes: str | None) -> bool:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE review_queue SET status = 'resolved', resolved_by = ?, resolution_notes = ?, updated_at = ?
            WHERE review_id = ? AND status = 'pending'
            """,
            (resolved_by, notes, now, review_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    # ── Bookings ──

    def save_booking(self, booking: Booking) -> None:
        self.conn.execute(
            """
            INSERT INTO bookings (
                booking_id, ticket_id, session_id, customer_name, customer_phone,
                service, booking_date, booking_time, duration_minutes,
                status, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(booking_id) DO UPDATE SET
                status=excluded.status, notes=excluded.notes, updated_at=excluded.updated_at
            """,
            (
                booking.booking_id, booking.ticket_id, booking.session_id,
                booking.customer_name, booking.customer_phone,
                booking.service, booking.booking_date, booking.booking_time,
                booking.duration_minutes, booking.status, booking.notes,
                booking.created_at.isoformat() if hasattr(booking.created_at, "isoformat") else booking.created_at,
                booking.updated_at.isoformat() if hasattr(booking.updated_at, "isoformat") else booking.updated_at,
            ),
        )
        self.conn.commit()

    def list_bookings_for_date(self, date: str) -> list[BookingSummary]:
        """List bookings for a specific date (YYYY-MM-DD)."""
        rows = self.conn.execute(
            """
            SELECT booking_id, ticket_id, customer_name, service, booking_date,
                   booking_time, status, created_at
            FROM bookings
            WHERE booking_date = ? AND status != 'cancelled'
            ORDER BY booking_time ASC
            """,
            (date,),
        ).fetchall()
        return [
            BookingSummary(
                booking_id=row["booking_id"], ticket_id=row["ticket_id"],
                customer_name=row["customer_name"], service=row["service"],
                booking_date=row["booking_date"], booking_time=row["booking_time"],
                status=row["status"], created_at=row["created_at"],
            )
            for row in rows
        ]

    def count_bookings_for_date(self, date: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM bookings WHERE booking_date = ? AND status != 'cancelled'",
            (date,),
        ).fetchone()
        return row["cnt"] if row else 0

    def list_bookings(self, limit: int = 50) -> list[BookingSummary]:
        rows = self.conn.execute(
            """
            SELECT booking_id, ticket_id, customer_name, service, booking_date,
                   booking_time, status, created_at
            FROM bookings
            WHERE status != 'cancelled'
            ORDER BY booking_date ASC, booking_time ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            BookingSummary(
                booking_id=row["booking_id"], ticket_id=row["ticket_id"],
                customer_name=row["customer_name"], service=row["service"],
                booking_date=row["booking_date"], booking_time=row["booking_time"],
                status=row["status"], created_at=row["created_at"],
            )
            for row in rows
        ]

    def cancel_booking(self, booking_id: str) -> bool:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            "UPDATE bookings SET status = 'cancelled', updated_at = ? WHERE booking_id = ?",
            (now, booking_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def list_unsubmitted_tickets(self, limit: int = 50) -> list[TicketSummary]:
        """Return confirmed tickets that failed or skipped OpenClaw submission."""
        rows = self.conn.execute(
            """
            SELECT ticket_id, session_id, status, customer_name, service, preferred_time,
                   submitted_to_openclaw, created_at, updated_at
            FROM tickets
            WHERE status IN ('confirmed', 'confirmed_local') AND submitted_to_openclaw = 0
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            TicketSummary(
                ticket_id=row["ticket_id"],
                session_id=row["session_id"],
                status=row["status"],
                customer_name=row["customer_name"],
                service=row["service"],
                preferred_time=row["preferred_time"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                submitted_to_openclaw=bool(row["submitted_to_openclaw"]),
            )
            for row in rows
        ]
