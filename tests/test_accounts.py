"""Music, calendar, email and the health check, against fake Spotify and Google servers."""

import asyncio
import base64
import json
import stat
import time

import httpx

from alfred.accounts import Account, Provider, save
from alfred.config import load_config
from alfred.google import Google, body_text
from alfred.health import Health, claude_problem
from alfred.music import Music, MusicError
from alfred.voice import spoken


class FakeAccount:
    """Answers like the real service: routes[(method, path)] -> JSON (or a status code)."""

    def __init__(self, routes: dict):
        self.routes, self.calls = routes, []

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        path = url.split(".com/v1", 1)[-1].split("googleapis.com", 1)[-1]
        self.calls.append((method, path, kwargs.get("params"), kwargs.get("json")))
        answer = self.routes.get((method, path), 204)
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, json=answer)


def player(playing=True, volume=60, supports_volume=True):
    return {
        "is_playing": playing,
        "device": {"id": "pi", "name": "Alfred", "volume_percent": volume,
                   "supports_volume": supports_volume},
        "item": {"name": "Clair de Lune", "artists": [{"name": "Debussy"}]},
    }  # fmt: skip


def volumes(account):
    return [c[2]["volume_percent"] for c in account.calls if c[1] == "/me/player/volume"]


def test_music_turns_down_while_alfred_listens_and_back_up_after():
    async def main():
        account = FakeAccount({("GET", "/me/player"): player(volume=60)})
        music = Music(account)
        await music.alfred_awake()
        await music.alfred_awake()  # already down: not again
        assert volumes(account) == [15]
        assert (await music.volume(80))["volume"] == 80  # while down: it's for afterwards
        await music.alfred_asleep()
        assert volumes(account) == [15, 80]

    asyncio.run(main())


def test_music_without_volume_control_is_paused_and_resumed():
    async def main():
        account = FakeAccount({("GET", "/me/player"): player(supports_volume=False)})
        music = Music(account)
        await music.alfred_awake()
        await music.alfred_asleep()
        assert [(m, p) for m, p, *_ in account.calls if m == "PUT"] == [
            ("PUT", "/me/player/pause"),
            ("PUT", "/me/player/play"),
        ]

    asyncio.run(main())


def test_music_plays_what_was_asked_on_the_named_speaker_turned_down():
    async def main():
        speakers = [
            {"id": "mac", "name": "MacBook", "is_active": True, "volume_percent": 90,
             "supports_volume": True},
            {"id": "pi", "name": "Alfred", "volume_percent": 40, "supports_volume": True},
        ]  # fmt: skip
        account = FakeAccount(
            {
                ("GET", "/me/player/devices"): {"devices": speakers},
                ("GET", "/search"): {"playlists": {"items": [None, {"uri": "spotify:playlist:1"}]}},
                ("GET", "/me/player"): player(),
            }
        )
        playing = await Music(account, speaker="alfred").play("lo-fi beats", "playlist")
        assert playing["title"] == "Clair de Lune" and playing["by"] == "Debussy"
        play = next(c for c in account.calls if c[1] == "/me/player/play")
        assert play[2] == {"device_id": "pi"} and play[3] == {"context_uri": "spotify:playlist:1"}
        assert volumes(account) == [10]  # Alfred is listening: it starts turned down

    asyncio.run(main())


def test_music_says_what_went_wrong():
    async def main():
        account = FakeAccount({("GET", "/me/player/devices"): {"devices": []}})
        try:
            await Music(account).play()
        except MusicError as error:
            assert "no Spotify speaker is on" in str(error)
        else:
            raise AssertionError("no speaker should be an error")

    asyncio.run(main())


def test_calendar_lists_shown_calendars_events_in_order(tmp_path):
    async def main():
        def events(*items):
            return {"items": [{"summary": t, "start": {"dateTime": s}} for t, s in items]}

        account = FakeAccount(
            {
                ("GET", "/calendar/v3/users/me/calendarList"): {
                    "items": [
                        {"id": "me", "summary": "Mine", "selected": True},
                        {"id": "ph#holiday", "summary": "Holidays", "selected": True},
                        {"id": "hidden", "selected": False},
                    ]
                },
                ("GET", "/calendar/v3/calendars/me/events"): events(("Lunch", "2026-11-21T12:00")),
                ("GET", "/calendar/v3/calendars/ph%23holiday/events"): events(
                    ("Standup", "2026-11-21T09:00")
                ),
            }
        )
        config = load_config({"OPENAI_API_KEY": "sk-test", "ALFRED_DATA": str(tmp_path)})
        found = await Google(account, config).events("2026-11-21")
        assert [e["title"] for e in found] == ["Standup", "Lunch"]
        assert found[0]["calendar"] == "Holidays"

    asyncio.run(main())


def test_an_emails_text_is_its_plain_part_else_its_html_without_tags():
    def part(mime, text):
        return {
            "mimeType": mime,
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        }

    both = {"parts": [part("text/html", "<b>Hi</b>"), part("text/plain", "Hi there")]}
    html_only = {"parts": [part("text/html", "<style>p{}</style><p>Pay &amp; go</p>")]}
    assert body_text(both) == "Hi there"
    assert body_text(html_only) == "Pay & go"


def test_logins_are_private_and_refreshed_when_they_expire(tmp_path):
    path = tmp_path / "spotify-login.json"
    save(path, {"access_token": "old", "refresh_token": "r1", "expires_in": 0})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    account = Account(Provider("Spotify", "a", "https://token", (), "id"), tmp_path)

    async def refresh(saved):
        return save(path, {"access_token": "new", "expires_in": 3600}, saved["refresh_token"])

    account._refresh = refresh
    assert asyncio.run(account._token()) == "new"
    assert json.loads(path.read_text())["refresh_token"] == "r1"  # kept when not resent
    assert json.loads(path.read_text())["expires_at"] > time.time()


def test_a_problem_is_shown_and_said_once(tmp_path):
    config = load_config({"OPENAI_API_KEY": "sk-test", "ALFRED_DATA": str(tmp_path)})
    health, heard = Health(config), []
    health.on_problem.append(heard.append)  # the announcer: said now, while talking
    error = "Claude Code returned an error result: API Error: 401 Invalid bearer token"
    health.failed("Claude", claude_problem(error))
    health.failed("Claude", claude_problem(error))  # the same again: not again
    assert heard == ["Claude"] and "claude setup-token" in health.problems["Claude"]
    assert len(health.untold()) == 1 and health.untold() == []  # or the next one opens with it
    health.ok("Claude")
    health.failed("Claude", claude_problem(error))  # broke again after working: said again
    assert heard == ["Claude", "Claude"] and len(health.untold()) == 1
    assert claude_problem("ValueError: bad JSON") is None  # not about reaching Claude


def test_sounds_are_left_out_of_what_was_said():
    assert spoken("Um. [breath] Right, [tongue click] sir.") == "Um. Right, sir."
    assert spoken("[grunt]") == ""


def test_the_self_check_reports_each_service_and_tells_nothing_twice(tmp_path):
    async def main():
        config = load_config({"OPENAI_API_KEY": "sk-test", "ALFRED_DATA": str(tmp_path)})
        health, heard = Health(config), []
        health.on_problem.append(heard.append)

        async def fine():
            return None

        async def logged_out():
            return "it isn't connected yet: run `uv run alfred-connect spotify`."

        health._openai, health._claude = fine, fine
        health.extra_checks["Spotify"] = logged_out
        result = await health.check(tell=False)  # asked out loud: the answer says it
        assert result["working"] == ["OpenAI", "Claude"] and "Spotify" in result["problems"]
        assert heard == [] and health.untold() == []

    asyncio.run(main())


def test_the_delegate_is_told_only_what_isnt_set_up():
    from alfred.tools import not_set_up

    assert "Music (Spotify) and calendar and email" in not_set_up(None, None)
    assert "Music" not in not_set_up(object(), None)
    assert not_set_up(object(), object()) == ""  # everything is: nothing to say
