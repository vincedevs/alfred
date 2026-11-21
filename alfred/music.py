"""Spotify by voice: play a song, artist, album or playlist; pause, skip, volume, and
what's playing. It plays on a Spotify speaker: the Spotify app on a computer or phone, or
the Pi itself with Raspotify. Needs Spotify Premium.

While Alfred is awake the music is turned down, so he hears you (his echo cancellation
removes only his own voice), and back up when he sleeps. A speaker without volume control
is paused instead, and resumed.
"""

import asyncio

import httpx

from alfred.accounts import Account, AccountError

API = "https://api.spotify.com/v1"
DUCK = 0.25  # the music's volume while Alfred is awake, as a share of yours
KINDS = {"song": "track", "artist": "artist", "album": "album", "playlist": "playlist"}
NO_SPEAKER = "no Spotify speaker is on: open Spotify on a device, or start Raspotify on the Pi."


class MusicError(Exception):
    """What went wrong, in words Alfred can say."""


class Music:
    def __init__(self, account: Account, speaker: str = ""):
        self._account, self._speaker_name = account, speaker.lower()
        self._yours: int | None = None  # your volume, while it's turned down for Alfred
        self._paused_for_alfred = False
        self._lock = asyncio.Lock()

    async def play(self, query: str = "", kind: str = "song") -> dict:
        """Play what's asked for (empty: carry on)."""
        body = {}
        if query:
            kind = KINDS.get(kind, "track")
            found = await self._api("GET", "/search", params={"q": query, "type": kind, "limit": 1})
            items = [i for i in found.get(f"{kind}s", {}).get("items", []) if i]
            if not items:
                raise MusicError(f"Spotify found no {kind} for {query!r}.")
            item = items[0]
            body = {"uris": [item["uri"]]} if kind == "track" else {"context_uri": item["uri"]}
        speaker = await self._speaker()
        async with self._lock:  # he's awake (it was asked for out loud): start it turned down
            self._paused_for_alfred = False
            if self._yours is None and speaker.get("supports_volume"):
                self._yours = speaker.get("volume_percent") or 50
                down = {"volume_percent": round(self._yours * DUCK), "device_id": speaker["id"]}
                await self._api("PUT", "/me/player/volume", params=down)
        await self._api("PUT", "/me/player/play", params={"device_id": speaker["id"]}, json=body)
        return await self.now_playing() if query else {"playing": True}

    async def pause(self) -> dict:
        await self._api("PUT", "/me/player/pause")
        self._paused_for_alfred = False
        return {"paused": True}

    async def skip(self, back: bool = False) -> dict:
        await self._api("POST", f"/me/player/{'previous' if back else 'next'}")
        await asyncio.sleep(0.5)  # Spotify takes a moment to say what's playing now
        return await self.now_playing()

    async def volume(self, percent: int) -> dict:
        percent = max(0, min(100, percent))
        async with self._lock:
            if self._yours is not None:  # turned down for Alfred: it comes back at this
                self._yours = percent
                return {"volume": percent, "note": "It's down while you talk; this is after."}
            await self._api("PUT", "/me/player/volume", params={"volume_percent": percent})
        return {"volume": percent}

    async def now_playing(self) -> dict:
        state = await self._api("GET", "/me/player")
        item = state.get("item") or {}
        if not item:
            return {"playing": False}
        return {
            "playing": state.get("is_playing", False),
            "title": item.get("name"),
            "by": ", ".join(a["name"] for a in item.get("artists", [])),
            "on": state.get("device", {}).get("name"),
        }

    async def alfred_awake(self) -> None:
        """Turn the music down (or pause it) while Alfred listens. Never raises."""
        async with self._lock:
            try:
                await self._turn_down()
            except (MusicError, AccountError, httpx.HTTPError):
                pass

    async def alfred_asleep(self) -> None:
        """Put the music back as it was. Never raises."""
        async with self._lock:
            yours, paused = self._yours, self._paused_for_alfred
            self._yours, self._paused_for_alfred = None, False
            try:
                if yours is not None:
                    await self._api("PUT", "/me/player/volume", params={"volume_percent": yours})
                if paused:
                    await self._api("PUT", "/me/player/play")
            except (MusicError, AccountError, httpx.HTTPError):
                pass

    async def _turn_down(self) -> None:
        if self._yours is not None or self._paused_for_alfred:
            return  # already down
        state = await self._api("GET", "/me/player")
        if not state.get("is_playing"):
            return
        device = state.get("device") or {}
        if device.get("supports_volume") and device.get("volume_percent"):
            self._yours = device["volume_percent"]
            down = round(self._yours * DUCK)
            await self._api("PUT", "/me/player/volume", params={"volume_percent": down})
        else:
            await self._api("PUT", "/me/player/pause")
            self._paused_for_alfred = True

    async def _speaker(self) -> dict:
        """Where to play: the named speaker, else the one playing, else the first one on."""
        devices = (await self._api("GET", "/me/player/devices")).get("devices", [])
        if not devices:
            raise MusicError(f"Spotify: {NO_SPEAKER}")
        named = [d for d in devices if d["name"].lower() == self._speaker_name]
        active = [d for d in devices if d.get("is_active")]
        return (named or active or devices)[0]

    async def _api(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = await self._account.request(method, API + path, **kwargs)
        except AccountError as error:
            raise MusicError(f"Spotify: {error}") from None
        if response.is_success:
            return response.json() if response.content else {}
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        reason = error.get("reason", "") if isinstance(error, dict) else ""
        if reason == "PREMIUM_REQUIRED":
            raise MusicError("Spotify only lets apps control Premium accounts.")
        if reason == "NO_ACTIVE_DEVICE" or response.status_code == 404:
            raise MusicError(f"Spotify: {NO_SPEAKER}")
        message = error.get("message", "") if isinstance(error, dict) else str(error)
        raise MusicError(f"Spotify refused that ({response.status_code}): {message[:150]}")
