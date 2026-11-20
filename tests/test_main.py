"""The wake loop: alarms ring until "Hey Alfred", and Ctrl+C ends Alfred cleanly."""

import asyncio
import sys
from types import SimpleNamespace

from alfred import __main__ as alfred_main


def test_the_closed_loop_message_is_hidden(monkeypatch):
    reported = []
    monkeypatch.setattr(sys, "__unraisablehook__", reported.append)
    closed = SimpleNamespace(exc_value=RuntimeError("Event loop is closed"))
    alfred_main.quiet_closed_loop_noise(closed)
    assert reported == []

    other = SimpleNamespace(exc_value=ValueError("something else"))
    alfred_main.quiet_closed_loop_noise(other)
    assert reported == [other]  # anything else is still reported


class Speaker:
    def __init__(self, words):
        self.mic, self.played, self.stopped = asyncio.Queue(), [], False
        for word in words:
            self.mic.put_nowait(word)

    def play(self, sound):
        self.played.append(sound)

    def queued_playback_seconds(self):
        return 0.0

    def stop_playback(self):
        self.stopped = True


class Ears:
    def process(self, audio):
        return audio == "hey alfred"


def test_an_alarm_rings_until_hey_alfred(monkeypatch):
    monkeypatch.setattr(alfred_main, "RING_SECONDS", 5)
    engine = Speaker(["noise", "hey alfred"])
    assert asyncio.run(alfred_main.ring(engine, Ears(), "beep")) is True
    assert engine.played and engine.stopped


def test_an_alarm_stops_ringing_after_a_while(monkeypatch):
    monkeypatch.setattr(alfred_main, "RING_SECONDS", 0.3)
    engine = Speaker([])
    assert asyncio.run(alfred_main.ring(engine, Ears(), "beep")) is False
    assert len(engine.played) > 1 and engine.stopped  # rang again and again, then stopped


def test_the_setup_script_parses_and_explains_itself():
    import subprocess

    from alfred.config import PROJECT_ROOT

    script = str(PROJECT_ROOT / "setup.sh")
    subprocess.run(["bash", "-n", script], check=True)
    shown = subprocess.run([script, "--help"], capture_output=True, text=True, check=True)
    assert "--no-office" in shown.stdout and "--service" in shown.stdout
