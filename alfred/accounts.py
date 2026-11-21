"""Your accounts that Alfred uses: Spotify (music) and Google (calendar and email).

`uv run alfred-connect spotify` (or `google`) logs in once. It's the standard OAuth flow,
done by hand so it works on a Pi with no screen: open the link on any device, approve,
then paste back the address the browser ends up on (that page won't load; that's
expected). Tokens are kept in the data folder, readable only by you, and refreshed as
needed. Alfred only reads Google; Spotify he can control.
"""

import asyncio
import base64
import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from alfred.config import Config, ConfigError, load_config

REDIRECT = "http://127.0.0.1:8765/callback"  # register exactly this in the app's settings
TIMEOUT = 10


class AccountError(Exception):
    """Not connected, or the login stopped working. The message says how to fix it."""


@dataclass(frozen=True)
class Provider:
    name: str
    auth_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str = ""  # Google's "desktop app" clients have one; Spotify uses PKCE
    extra: tuple[tuple[str, str], ...] = ()  # more login parameters

    @property
    def fix(self) -> str:
        return f"run `uv run alfred-connect {self.name.lower()}`"


def spotify(config: Config) -> Provider | None:
    if not config.spotify_client_id:
        return None
    scopes = ("user-read-playback-state", "user-modify-playback-state")
    return Provider(
        "Spotify",
        "https://accounts.spotify.com/authorize",
        "https://accounts.spotify.com/api/token",
        scopes,
        config.spotify_client_id,
    )


def google(config: Config) -> Provider | None:
    if not (config.google_client_id and config.google_client_secret):
        return None
    scopes = (
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
    )
    return Provider(
        "Google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        scopes,
        config.google_client_id,
        config.google_client_secret,
        (("access_type", "offline"), ("prompt", "consent")),  # a refresh token, every time
    )


class Account:
    """A connected account: authorized requests, refreshing the token when it expires."""

    def __init__(self, provider: Provider, data_dir: Path):
        self.provider = provider
        self._path = data_dir / f"{provider.name.lower()}-login.json"
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._path.is_file()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for attempt in range(2):
                headers = {"Authorization": f"Bearer {await self._token(fresh=attempt > 0)}"}
                response = await client.request(method, url, headers=headers, **kwargs)
                if response.status_code != 401:
                    return response
        return response

    async def check(self) -> str | None:
        """For the health check: None if the login works."""
        try:
            await self._token(fresh=True)
        except AccountError as error:
            return str(error)
        except httpx.HTTPError:
            return "I can't reach it: check the internet connection."
        return None

    async def _token(self, fresh: bool = False) -> str:
        async with self._lock:
            if not self.connected:
                raise AccountError(f"it isn't connected yet: {self.provider.fix}.")
            saved = json.loads(self._path.read_text())
            if fresh or saved["expires_at"] < time.time() + 60:
                saved = await self._refresh(saved)
            return saved["access_token"]

    async def _refresh(self, saved: dict) -> dict:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": saved["refresh_token"],
            "client_id": self.provider.client_id,
        } | ({"client_secret": self.provider.client_secret} if self.provider.client_secret else {})
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(self.provider.token_url, data=form)
        if response.status_code in (400, 401):  # revoked, expired, or a changed client
            raise AccountError(f"its login stopped working: {self.provider.fix}.")
        response.raise_for_status()
        return save(self._path, response.json(), saved["refresh_token"])


def save(path: Path, token: dict, refresh_token: str = "") -> dict:
    """Keep a token response, readable only by you."""
    saved = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token") or refresh_token,  # may come back new
        "expires_at": time.time() + float(token.get("expires_in", 3600)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(json.dumps(saved))
    return saved


def connect(provider: Provider, data_dir: Path) -> None:
    """Log in by hand: print the link, read back the address the browser ended on."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    link = f"{provider.auth_url}?" + urlencode(
        {
            "client_id": provider.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": " ".join(provider.scopes),
            "state": state,
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            **dict(provider.extra),
        }
    )
    print(f"1. Open this link on any device and approve:\n\n{link}\n")
    print(f"2. The browser then goes to {REDIRECT}... and shows an error page: that's fine.")
    pasted = input("   Paste that page's whole address here: ").strip()
    query = parse_qs(urlsplit(pasted).query)
    if query.get("state") != [state] or "code" not in query:
        sys.exit(f"That isn't the address from this login ({query.get('error', ['no code'])[0]}).")
    form = {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "redirect_uri": REDIRECT,
        "client_id": provider.client_id,
        "code_verifier": verifier,
    } | ({"client_secret": provider.client_secret} if provider.client_secret else {})
    response = httpx.post(provider.token_url, data=form, timeout=TIMEOUT)
    if not response.is_success:
        sys.exit(f"{provider.name} refused the login: {response.text[:300]}")
    token = response.json()
    if not token.get("refresh_token"):
        sys.exit(f"{provider.name} gave no lasting login; try again.")
    save(data_dir / f"{provider.name.lower()}-login.json", token)
    print(f"Connected {provider.name}. Alfred can use it now.")


def main() -> None:
    """`uv run alfred-connect spotify|google`"""
    which = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    try:
        config = load_config()
    except ConfigError as error:
        sys.exit(f"Config error: {error}")
    makers = {"spotify": spotify, "google": google}
    if which not in makers:
        sys.exit("Usage: uv run alfred-connect spotify|google")
    provider = makers[which](config)
    if provider is None:
        needs = "SPOTIFY_CLIENT_ID" if which == "spotify" else "GOOGLE_CLIENT_ID and _SECRET"
        sys.exit(f"Set {needs} in .env first (see README: Music, calendar and email).")
    connect(provider, config.data_dir)
