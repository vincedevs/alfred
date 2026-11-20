"""Reminders, timers and alarms: set, listed, cancelled, due, and said."""

import asyncio
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_conversation import FakeLLM, Params, services

from alfred.announcer import Announcer, opening_messages
from alfred.db import Database
from alfred.questions import Questions
from alfred.reminders import Reminders, next_time

MANILA = ZoneInfo("Asia/Manila")


def reminders(tmp_path) -> tuple[Database, Reminders]:
    db = Database(tmp_path / "alfred.db")
    return db, Reminders(db, MANILA)


def test_set_list_and_cancel(tmp_path):
    _, r = reminders(tmp_path)
    timer = r.add("take the pasta off", "timer", in_minutes=10)
    tomorrow = (datetime.now(MANILA) + timedelta(days=1)).replace(hour=7, minute=0)
    alarm = r.add("wake up", "alarm", at=tomorrow.strftime("%Y-%m-%dT%H:%M"))
    assert abs(timer.due_at - (time.time() + 600)) < 2
    assert alarm.due_at == tomorrow.replace(second=0, microsecond=0).timestamp()
    assert [x.id for x in r.upcoming()] == [timer.id, alarm.id]  # soonest first
    assert r.describe(alarm)["when"].endswith(", 7:00 AM") and r.due() == []
    assert r.cancel(timer.id) and not r.cancel(timer.id)
    assert [x.id for x in r.upcoming()] == [alarm.id]


def test_bad_times_are_refused(tmp_path):
    _, r = reminders(tmp_path)
    with pytest.raises(ValueError, match="already passed"):
        r.add("call mum", "reminder", at="2020-01-01T09:00")
    with pytest.raises(ValueError, match="give a time"):
        r.add("call mum", "reminder")
    with pytest.raises(ValueError, match="kind"):
        r.add("call mum", "nudge", in_minutes=5)


def test_the_wait_ends_when_one_is_due(tmp_path):
    async def main():
        _, r = reminders(tmp_path)
        waiting = asyncio.create_task(r.wait_until_due())
        await asyncio.sleep(0.02)
        assert not waiting.done()  # nothing set: waits
        r.add("stretch", "timer", in_minutes=0.05 / 60)  # the new one re-plans the wait
        await asyncio.wait_for(waiting, 1)
        assert [x.text for x in r.due()] == ["stretch"]

    asyncio.run(main())


def test_due_reminders_open_the_next_conversation(tmp_path):
    db, r = reminders(tmp_path)
    reminder = db.add_reminder("call the bank", "reminder", time.time() - 3600)  # missed
    messages, told, said = opening_messages(db, Questions(db), r)
    assert (
        "call the bank" in messages[0]["content"]
        and "while you were away" in messages[0]["content"]
    )
    assert said == [reminder.id] and told == []
    r.mark_said(said)
    assert r.due() == [] and r.upcoming() == []


def test_a_reminder_due_while_talking_is_said(tmp_path, monkeypatch):
    async def main():
        db, questions, agent, _ = services(tmp_path, None)
        r = Reminders(db, MANILA)
        rang = []
        announcer = Announcer(FakeLLM(), db, agent, questions, r, ring=rang.append)
        notes = []
        monkeypatch.setattr(announcer, "tell", notes.append)
        announcer.session_open = True
        announcer.start()
        r.add("check the oven", "timer", in_minutes=0.03 / 60)
        await asyncio.sleep(0.2)
        assert "check the oven" in notes[-1] and r.due() == []
        assert rang[0][0].kind == "timer"  # it sounds too, not only said
        announcer.stop()

    asyncio.run(main())


def test_the_delegate_sets_reminders(tmp_path):
    async def main():
        _, _, _, tools = services(tmp_path, None)
        params = Params()
        await tools["remind"](params, text="tea", kind="timer", in_minutes=4)
        assert params.result["set"]["text"] == "tea"
        await tools["list_reminders"](params)
        (upcoming,) = params.result["upcoming"]
        await tools["cancel_reminder"](params, reminder_id=upcoming["reminder_id"])
        assert params.result == {"cancelled": True}
        await tools["remind"](params, text="tea", kind="timer")
        assert "error" in params.result

    asyncio.run(main())


def test_a_repeating_reminder_moves_on_once_said(tmp_path, monkeypatch):
    db, r = reminders(tmp_path)
    friday_7 = datetime(2026, 11, 20, 7, 0, tzinfo=MANILA)  # a Friday
    reminder = db.add_reminder("stand-up", "alarm", friday_7.timestamp(), "weekdays")
    monkeypatch.setattr(time, "time", lambda: friday_7.timestamp() + 60)
    r.mark_said([reminder.id])
    (moved,) = r.upcoming()
    assert datetime.fromtimestamp(moved.due_at, MANILA) == datetime(
        2026, 11, 23, 7, 0, tzinfo=MANILA
    )
    assert moved.status == "pending"  # Monday at 7, still going


def test_next_times():
    saturday_9 = datetime(2026, 11, 21, 9, 0, tzinfo=MANILA)
    seven = saturday_9.replace(hour=7)
    after = saturday_9.timestamp()
    nxt = lambda repeat: datetime.fromtimestamp(  # noqa: E731
        next_time(seven, repeat, after, include=True), MANILA
    )
    assert nxt("daily") == datetime(2026, 11, 22, 7, 0, tzinfo=MANILA)  # today's 7 has passed
    assert nxt("weekdays") == datetime(2026, 11, 23, 7, 0, tzinfo=MANILA)  # Monday
    assert nxt("weekends") == datetime(2026, 11, 22, 7, 0, tzinfo=MANILA)  # Sunday
    assert nxt("weekly") == datetime(2026, 11, 28, 7, 0, tzinfo=MANILA)  # next Saturday
    missed_a_week = seven.timestamp() + 7 * 86400 + 3600
    assert next_time(seven, "daily", missed_a_week) == seven.timestamp() + 8 * 86400  # once


def test_daylight_saving_keeps_the_clock_time():
    london = ZoneInfo("Europe/London")
    before = datetime(2026, 10, 24, 7, 0, tzinfo=london)  # clocks go back on the 25th
    after = datetime.fromtimestamp(next_time(before, "daily", before.timestamp()), london)
    assert (after.day, after.hour) == (25, 7)


def test_repeats_are_checked(tmp_path):
    _, r = reminders(tmp_path)
    with pytest.raises(ValueError, match="timer can't repeat"):
        r.add("tea", "timer", in_minutes=5, repeat="daily")
    with pytest.raises(ValueError, match="repeat must be"):
        r.add("tea", "reminder", in_minutes=5, repeat="hourly")
    every_day = r.add("vitamins", "reminder", at="2020-01-01T08:00", repeat="daily")
    assert every_day.due_at > time.time() and r.describe(every_day)["repeat"] == "daily"


def test_alfred_wakes_up_once_for_each_due_reminder(tmp_path):
    async def main():
        db, r = reminders(tmp_path)
        db.add_reminder("call the bank", "reminder", time.time() - 5)
        await asyncio.wait_for(r.wait_until_due(), 1)  # due: wake up
        r.spoke_up_for(r.new_due())  # ...but say GPT-Live couldn't be reached
        waiting = asyncio.create_task(r.wait_until_due())
        await asyncio.sleep(0.05)
        assert not waiting.done() and r.new_due() == []  # no ringing again and again
        assert [x.text for x in r.due()] == ["call the bank"]  # still said next time
        waiting.cancel()

    asyncio.run(main())
