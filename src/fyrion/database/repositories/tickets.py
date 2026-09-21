"""
Ticket repository.

Ticket rows live in the core ``tickets`` table, so those reads and writes go
through the pooled data layer, which validates every identifier against the
schema allow-list and binds every value as a SQL parameter.

The canonical configuration (category, log channel, support role) lives in
``guild_settings``; writes are mirrored into the legacy ``ticket_configs`` row so
anything still reading that table keeps working.

Panel presentation — the title, description and the dynamic topic buttons — has
no core table, so this module owns one. The DDL is idempotent and applied on cog
load, and the table cascades from ``guild_settings`` so removing a guild leaves
no orphaned rows. Topics are stored as a JSON array because they are only ever
read as a whole; nothing queries or counts them individually.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar, Final, Mapping, Sequence

from fyrion.database.manager import utc_now_iso

log = logging.getLogger("fyrion.database.repositories.tickets")

# Idempotent DDL, executed statement by statement so the repository only needs a
# database object exposing ``execute``.
PANEL_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS ticket_panels (
        guild_id    INTEGER PRIMARY KEY,
        channel_id  INTEGER,
        message_id  INTEGER,
        title       TEXT    NOT NULL DEFAULT 'Support Tickets',
        description TEXT,
        topics      TEXT    NOT NULL DEFAULT '[]',
        created_by  INTEGER,
        updated_at  TEXT    NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        FOREIGN KEY (guild_id) REFERENCES guild_settings (guild_id)
            ON DELETE CASCADE
    )
    """,
)

MAX_TOPICS_STORED = 5
MAX_TITLE = 256
MAX_DESCRIPTION = 2000
MAX_SUBJECT = 100
MAX_REASON = 500

OPEN_STATUSES: Final[tuple[str, ...]] = ("open", "claimed")


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class TicketRepository:
    """Reads and writes ticket configuration, panels and ticket rows."""

    _schema_ready: ClassVar[bool] = False

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def ensure_schema(self, *, force: bool = False) -> None:
        """Creates the panel table if it does not exist yet."""
        if TicketRepository._schema_ready and not force:
            return
        for statement in PANEL_STATEMENTS:
            await self.db.execute(statement)
        TicketRepository._schema_ready = True

    @classmethod
    def reset_schema_flag(cls) -> None:
        """Forces the next :meth:`ensure_schema` call to run. Used by tests."""
        cls._schema_ready = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        """Returns the effective ticket configuration for a guild."""
        guild_id = int(guild_id)
        settings = await self.db.get_guild_settings(guild_id)

        config: dict[str, Any] = {
            "guild_id": guild_id,
            "category_id": _as_int(settings.get("ticket_category_id")),
            "log_channel_id": _as_int(settings.get("ticket_log_channel_id")),
            "support_role_id": _as_int(settings.get("ticket_support_role_id")),
        }

        # The legacy row only fills gaps; an explicit new-style value wins.
        if config["category_id"] is None or config["log_channel_id"] is None:
            legacy = await self.db.fetchrow(
                "SELECT category_id, log_channel_id FROM ticket_configs "
                "WHERE guild_id = ?",
                (guild_id,),
            )
            if legacy is not None:
                if config["category_id"] is None:
                    config["category_id"] = _as_int(legacy["category_id"])
                if config["log_channel_id"] is None:
                    config["log_channel_id"] = _as_int(legacy["log_channel_id"])

        await self.ensure_schema()
        panel = await self.db.fetchrow(
            "SELECT channel_id, message_id, title, description, topics "
            "FROM ticket_panels WHERE guild_id = ?",
            (guild_id,),
        )
        if panel is not None:
            config.update(
                {
                    "panel_channel_id": _as_int(panel["channel_id"]),
                    "panel_message_id": _as_int(panel["message_id"]),
                    "title": panel["title"],
                    "description": panel["description"],
                    "topics": panel["topics"],
                }
            )
        else:
            config.update(
                {
                    "panel_channel_id": None,
                    "panel_message_id": None,
                    "title": None,
                    "description": None,
                    "topics": "[]",
                }
            )

        return config

    async def set_config(
        self,
        guild_id: int,
        category_id: int | None,
        log_channel_id: int | None = None,
        support_role_id: int | None = None,
    ) -> None:
        """Writes the ticket configuration to both storage layers."""
        guild_id = int(guild_id)
        await self.db.update_guild_settings(
            guild_id,
            ticket_category_id=_as_int(category_id),
            ticket_log_channel_id=_as_int(log_channel_id),
            ticket_support_role_id=_as_int(support_role_id),
        )

        await self.db.execute(
            "INSERT INTO ticket_configs (guild_id, category_id, log_channel_id) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "    category_id = excluded.category_id, "
            "    log_channel_id = excluded.log_channel_id",
            (guild_id, _as_int(category_id), _as_int(log_channel_id)),
        )

    # ------------------------------------------------------------------
    # Panels
    # ------------------------------------------------------------------

    async def save_panel(
        self,
        guild_id: int,
        *,
        channel_id: int | None,
        message_id: int | None,
        title: str,
        description: str | None,
        topics: Sequence[Mapping[str, Any]],
        created_by: int | None = None,
    ) -> None:
        """Stores the panel presentation for a guild."""
        guild_id = int(guild_id)
        await self.ensure_schema()
        await self.db.ensure_guild(guild_id)

        payload = json.dumps(
            [dict(topic) for topic in list(topics)[:MAX_TOPICS_STORED]],
            separators=(",", ":"),
            ensure_ascii=False,
        )

        await self.db.execute(
            "INSERT INTO ticket_panels "
            "    (guild_id, channel_id, message_id, title, description, topics, "
            "     created_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "    channel_id = excluded.channel_id, "
            "    message_id = excluded.message_id, "
            "    title = excluded.title, "
            "    description = excluded.description, "
            "    topics = excluded.topics, "
            "    created_by = excluded.created_by, "
            "    updated_at = excluded.updated_at",
            (
                guild_id,
                _as_int(channel_id),
                _as_int(message_id),
                str(title)[:MAX_TITLE],
                str(description)[:MAX_DESCRIPTION] if description else None,
                payload,
                _as_int(created_by),
                utc_now_iso(),
            ),
        )

    async def get_panel(self, guild_id: int) -> dict[str, Any] | None:
        await self.ensure_schema()
        row = await self.db.fetchrow(
            "SELECT * FROM ticket_panels WHERE guild_id = ?", (int(guild_id),)
        )
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    async def create_ticket(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        subject: str | None = None,
        panel_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Opens a ticket with a per-guild sequential number."""
        return await self.db.create_support_ticket(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            user_id=int(user_id),
            subject=str(subject)[:MAX_SUBJECT] if subject else None,
            panel_message_id=_as_int(panel_message_id),
        )

    async def get_ticket_by_channel(self, channel_id: int) -> dict[str, Any] | None:
        return await self.db.fetch_one("tickets", {"channel_id": int(channel_id)})

    async def get_open_ticket_for_user(
        self, guild_id: int, user_id: int
    ) -> dict[str, Any] | None:
        return await self.db.get_open_ticket(int(guild_id), int(user_id))

    async def list_tickets(
        self,
        guild_id: int,
        *,
        status: str | Sequence[str] | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"guild_id": int(guild_id)}
        if status is not None:
            where["status"] = status
        return await self.db.fetch_many(
            "tickets",
            where,
            # Transcripts can be large and are never needed for a list view.
            columns=[
                "ticket_id",
                "guild_id",
                "ticket_number",
                "channel_id",
                "user_id",
                "subject",
                "status",
                "claimed_by",
                "closed_by",
                "close_reason",
                "created_at",
                "closed_at",
            ],
            order_by="ticket_id DESC",
            limit=max(1, int(limit)),
        )

    async def count_open(self, guild_id: int) -> int:
        return await self.db.count(
            "tickets", {"guild_id": int(guild_id), "status": list(OPEN_STATUSES)}
        )

    async def claim_ticket(self, channel_id: int, claimed_by: int) -> bool:
        """Claims an unclaimed ticket. Returns False when it was already claimed.

        The status guard is part of the ``UPDATE``, so two staff members pressing
        claim simultaneously cannot both succeed.
        """
        return await self.db.claim_ticket(int(channel_id), int(claimed_by))

    async def release_ticket(self, channel_id: int) -> bool:
        """Returns a claimed ticket to the unclaimed queue."""
        changed = await self.db.update(
            "tickets",
            {"status": "open", "claimed_by": None},
            {"channel_id": int(channel_id), "status": "claimed"},
        )
        return bool(changed)

    async def close_ticket(
        self,
        channel_id: int,
        *,
        closed_by: int | None = None,
        reason: str | None = None,
        transcript: str | None = None,
    ) -> bool:
        """Marks a ticket closed. Returns False when it was already closed."""
        return await self.db.close_support_ticket(
            int(channel_id),
            closed_by=_as_int(closed_by),
            close_reason=str(reason)[:MAX_REASON] if reason else None,
            transcript=transcript,
        )

    async def set_transcript(self, channel_id: int, transcript: str) -> bool:
        changed = await self.db.update(
            "tickets", {"transcript": transcript}, {"channel_id": int(channel_id)}
        )
        return bool(changed)


__all__ = [
    "TicketRepository",
    "PANEL_STATEMENTS",
    "OPEN_STATUSES",
    "MAX_TOPICS_STORED",
]
