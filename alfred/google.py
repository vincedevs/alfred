"""Your Google calendar and Gmail, read-only: what's on, and finding and reading email.
Alfred can't change either (accounts.py asks for read access only)."""

import base64
import html
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

from alfred.accounts import Account, AccountError
from alfred.config import Config

CALENDAR = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_EVENTS = 25
MAX_EMAILS = 10
EMAIL_CHARACTERS = 4000  # of one email's text, for the delegate


class GoogleError(Exception):
    """What went wrong, in words Alfred can say."""


class Google:
    def __init__(self, account: Account, config: Config):
        self._account, self._config = account, config

    async def events(self, start: str = "", days: int = 1) -> list[dict]:
        """The events on every calendar you show, from `start` (a date; empty: today)."""
        first = date.fromisoformat(start) if start else self._config.now().date()
        since = datetime.combine(first, datetime.min.time(), self._config.tz)
        until = since + timedelta(days=max(1, min(days, 31)))
        calendars = await self._get(f"{CALENDAR}/users/me/calendarList")
        found = []
        for calendar in calendars.get("items", []):
            if not calendar.get("selected"):
                continue
            listed = await self._get(
                f"{CALENDAR}/calendars/{quote(calendar['id'], safe='')}/events",
                timeMin=since.isoformat(),
                timeMax=until.isoformat(),
                singleEvents="true",
                orderBy="startTime",
                maxResults=MAX_EVENTS,
            )
            found += [event_text(e, calendar.get("summary", "")) for e in listed.get("items", [])]
        return sorted(found, key=lambda e: e["starts"])[:MAX_EVENTS]

    async def emails(self, search: str = "is:unread in:inbox", limit: int = 5) -> list[dict]:
        """Emails matching a Gmail search, newest first: who, subject, when, a snippet."""
        listed = await self._get(
            f"{GMAIL}/messages", q=search, maxResults=max(1, min(limit, MAX_EMAILS))
        )
        found = []
        for message in listed.get("messages", []):
            full = await self._get(
                f"{GMAIL}/messages/{message['id']}",
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
            found.append(
                {
                    "id": message["id"],
                    "from": headers.get("From", ""),
                    "subject": headers.get("Subject", "(no subject)"),
                    "date": headers.get("Date", ""),
                    "snippet": html.unescape(full.get("snippet", "")),
                    "unread": "UNREAD" in full.get("labelIds", []),
                }
            )
        return found

    async def email(self, message_id: str) -> dict:
        """One email's text."""
        full = await self._get(f"{GMAIL}/messages/{message_id}", format="full")
        payload = full.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        text = body_text(payload)
        return {
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "text": text[:EMAIL_CHARACTERS],
            "cut": len(text) > EMAIL_CHARACTERS,
        }

    async def _get(self, url: str, **params) -> dict:
        try:
            response = await self._account.request("GET", url, params=params)
        except AccountError as error:
            raise GoogleError(f"Google: {error}") from None
        if response.status_code == 403:
            raise GoogleError("Google refused: turn on the Calendar and Gmail APIs for the app.")
        if response.status_code == 404:
            raise GoogleError("Google found no such email or calendar.")
        if not response.is_success:
            raise GoogleError(f"Google answered with an error ({response.status_code}).")
        return response.json()


def event_text(event: dict, calendar: str) -> dict:
    start, end = event.get("start", {}), event.get("end", {})
    return {
        "title": event.get("summary", "(no title)"),
        "starts": start.get("dateTime") or start.get("date", ""),
        "ends": end.get("dateTime") or end.get("date", ""),
        "all_day": "date" in start,
        "where": event.get("location", ""),
        "calendar": calendar,
    }


def body_text(part: dict) -> str:
    """An email's plain text: its text/plain part, else its HTML without the tags."""
    plain, rich = [], []

    def walk(p: dict) -> None:
        data = p.get("body", {}).get("data")
        if data:
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(errors="replace")
            {"text/plain": plain, "text/html": rich}.get(p.get("mimeType"), []).append(text)
        for child in p.get("parts", []):
            walk(child)

    walk(part)
    if plain:
        return "\n".join(plain).strip()
    words = re.sub(r"<(style|script)\b.*?</\1>|<[^>]+>", " ", "\n".join(rich), flags=re.S | re.I)
    return re.sub(r"[ \t]+", " ", html.unescape(words)).strip()
