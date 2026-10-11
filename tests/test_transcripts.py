"""
Tests for the plain-text ticket transcript renderer.

Messages, channels and users are lightweight fakes: the renderer only reads a
handful of attributes, so no Discord connection or mocks are needed.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fyrion.utils.transcripts import (
    MAX_ATTACHMENTS_LISTED,
    MAX_MESSAGE_CONTENT,
    MAX_MESSAGES,
    RULE,
    _clean,
    _stamp,
    build_transcript,
    collect_history,
    render_message,
    transcript_filename,
)

NOON = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


class FakeUser:
    """A user whose ``str()`` mimics a Discord username."""

    def __init__(self, user_id: int, name: str, bot: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.bot = bot

    def __str__(self) -> str:
        return self.name


def make_message(content="hello", **overrides):
    fields = {
        "author": FakeUser(42, "alice"),
        "created_at": NOON,
        "edited_at": None,
        "content": content,
        "attachments": [],
        "embeds": [],
        "stickers": [],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_attachment(index: int):
    return SimpleNamespace(
        filename=f"file{index}.png",
        size=100 + index,
        url=f"https://cdn.example/file{index}.png",
    )


GUILD = SimpleNamespace(name="Test Server", id=1)
CHANNEL = SimpleNamespace(name="ticket-0007", id=2)


def test_stamp_formats_utc():
    assert _stamp(NOON) == "2026-10-11 12:00:00 UTC"


def test_stamp_converts_other_timezones_to_utc():
    ist = timezone(timedelta(hours=5, minutes=30))
    moment = datetime(2026, 1, 1, 5, 30, 0, tzinfo=ist)
    assert _stamp(moment) == "2026-01-01 00:00:00 UTC"


def test_stamp_handles_missing_and_unusable_values():
    assert _stamp(None) == "unknown time"
    assert _stamp("not a datetime") == "not a datetime"


def test_clean_normalises_line_endings():
    assert _clean("a\r\nb\rc") == "a\nb\nc"


def test_clean_handles_empty_text():
    assert _clean("") == ""


def test_clean_bounds_long_messages():
    cleaned = _clean("x" * (MAX_MESSAGE_CONTENT + 50))
    assert cleaned == "x" * MAX_MESSAGE_CONTENT + " [truncated]"
    assert _clean("x" * MAX_MESSAGE_CONTENT) == "x" * MAX_MESSAGE_CONTENT


def test_render_message_basic_layout():
    rendered = render_message(make_message("hello\r\nthere"))
    assert rendered.splitlines() == [
        "[2026-10-11 12:00:00 UTC] alice (42):",
        "    hello",
        "    there",
    ]


def test_render_message_marks_bots_and_edits():
    message = make_message(
        author=FakeUser(7, "helper", bot=True),
        edited_at=NOON + timedelta(minutes=5),
    )
    header = render_message(message).splitlines()[0]
    assert header == (
        "[2026-10-11 12:00:00 UTC] helper (7) [bot] "
        "[edited 2026-10-11 12:05:00 UTC]:"
    )


def test_render_message_placeholder_for_empty_message():
    rendered = render_message(make_message(""))
    assert rendered.splitlines()[-1] == "    [no text content]"


def test_render_message_lists_attachments_without_a_placeholder():
    message = make_message("", attachments=[make_attachment(1)])
    lines = render_message(message).splitlines()
    assert "    [no text content]" not in lines
    assert lines[-1] == (
        "    [attachment] file1.png (101 bytes) https://cdn.example/file1.png"
    )


def test_render_message_caps_the_attachment_list():
    total = MAX_ATTACHMENTS_LISTED + 3
    attachments = [make_attachment(i) for i in range(total)]
    message = make_message("see files", attachments=attachments)
    lines = render_message(message).splitlines()
    listed = [line for line in lines if line.startswith("    [attachment] file")]
    assert len(listed) == MAX_ATTACHMENTS_LISTED
    assert lines[-1] == "    [attachment] ... and 3 more"


def test_render_message_summarises_embeds_and_stickers():
    embeds = [
        SimpleNamespace(title="Titled", author=None, description="ignored"),
        SimpleNamespace(title=None, author=None, description="first\nsecond"),
        SimpleNamespace(title=None, author=None, description=""),
    ]
    stickers = [SimpleNamespace(name="wave")]
    lines = render_message(make_message("", embeds=embeds, stickers=stickers))
    lines = lines.splitlines()
    assert lines[1:] == [
        "    [embed] Titled",
        "    [embed] first",
        "    [embed] embed",
        "    [sticker] wave",
    ]


def test_render_message_truncates_overlong_content():
    rendered = render_message(make_message("y" * (MAX_MESSAGE_CONTENT + 10)))
    assert rendered.endswith(" [truncated]")


def test_transcript_filename_uses_the_ticket_number_when_known():
    assert transcript_filename({"ticket_number": 7}, CHANNEL) == "ticket-0007-2.txt"
    name = transcript_filename({"ticket_number": 12345}, CHANNEL)
    assert name == "ticket-12345-2.txt"


@pytest.mark.parametrize("ticket", [None, {}, {"ticket_number": 0}])
def test_transcript_filename_falls_back_to_the_channel_id(ticket):
    assert transcript_filename(ticket, CHANNEL) == "ticket-2.txt"


def test_build_transcript_for_an_empty_ticket():
    text = build_transcript(guild=GUILD, channel=CHANNEL, messages=[])
    lines = text.splitlines()
    assert lines[0] == "Fyrion support ticket transcript"
    assert lines[1] == RULE
    assert "Server        : Test Server (1)" in lines
    assert "Channel       : #ticket-0007 (2)" in lines
    assert "Messages      : 0" in lines
    assert "[No messages were recorded in this ticket.]" in lines
    assert lines[-2:] == [RULE, "End of transcript."]


def test_build_transcript_includes_ticket_details():
    text = build_transcript(
        guild=GUILD,
        channel=CHANNEL,
        messages=[make_message("first"), make_message("second")],
        ticket={
            "ticket_number": 7,
            "ticket_id": 99,
            "subject": "Billing\r\nhelp",
            "user_id": 42,
            "created_at": "2026-10-11T11:00:00Z",
            "claimed_by": 555,
        },
        closed_by=FakeUser(1000, "staffer"),
        reason="resolved",
        note="exported by test",
    )
    lines = text.splitlines()
    assert "Ticket        : #7" in lines
    assert "Ticket ID     : 99" in lines
    assert "Topic         : Billing" in lines
    assert "Opened by     : user 42" in lines
    assert "Opened at     : 2026-10-11T11:00:00Z" in lines
    assert "Claimed by    : user 555" in lines
    assert "Closed by     : staffer (1000)" in lines
    assert "Close reason  : resolved" in lines
    assert "Messages      : 2" in lines
    assert "Note          : exported by test" in lines
    assert "    first" in lines
    assert "    second" in lines


def test_build_transcript_prefers_the_opener_object_over_the_stored_id():
    text = build_transcript(
        guild=GUILD,
        channel=CHANNEL,
        messages=[],
        ticket={"user_id": 42},
        opener=FakeUser(42, "alice"),
    )
    lines = text.splitlines()
    assert "Opened by     : alice (42)" in lines
    assert "Opened by     : user 42" not in lines


def test_build_transcript_flags_truncation():
    text = build_transcript(
        guild=GUILD, channel=CHANNEL, messages=[make_message()], truncated=True
    )
    assert "Messages      : 1 (truncated)" in text
    banner = f"[Only the first {MAX_MESSAGES} messages of this ticket are included.]"
    assert banner in text


def test_build_transcript_omits_the_truncation_banner_when_complete():
    text = build_transcript(guild=GUILD, channel=CHANNEL, messages=[make_message()])
    assert "truncated" not in text
    assert "Only the first" not in text


class FakeChannel:
    """A channel whose ``history`` honours ``limit`` like Discord's does."""

    def __init__(self, messages) -> None:
        self._messages = list(messages)
        self.requested_limit = None

    async def history(self, *, limit, oldest_first):
        assert oldest_first is True
        self.requested_limit = limit
        for message in self._messages[:limit]:
            yield message


@pytest.mark.asyncio
async def test_collect_history_reports_no_truncation_when_everything_fits():
    channel = FakeChannel(range(5))
    messages, truncated = await collect_history(channel, limit=5)
    assert messages == [0, 1, 2, 3, 4]
    assert truncated is False
    # One extra message is requested so truncation is detected, not guessed.
    assert channel.requested_limit == 6


@pytest.mark.asyncio
async def test_collect_history_reports_truncation_and_trims_the_extra_message():
    channel = FakeChannel(range(10))
    messages, truncated = await collect_history(channel, limit=3)
    assert messages == [0, 1, 2]
    assert truncated is True


@pytest.mark.asyncio
async def test_collect_history_clamps_a_nonsensical_limit_to_one():
    channel = FakeChannel(range(4))
    messages, truncated = await collect_history(channel, limit=0)
    assert messages == [0]
    assert truncated is True
