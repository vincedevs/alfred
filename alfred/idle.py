"""When a conversation closes. GPT-Live bills for every open second, silence included.

After about `idle_seconds` without Alfred speaking or working, a soft tone plays; if
nobody speaks within WARNING_SECONDS, the session closes. Speaking after the tone keeps it
open and starts the count again. While the agent works or waits for your answer, it stays
open up to KEEP_OPEN_SECONDS. Told to go to sleep, it closes once his goodbye has ended.
"""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import EndFrame

WARNING_SECONDS = 5  # the tone plays this long before closing
KEEP_OPEN_SECONDS = 120  # most quiet time while the agent is busy
GOODBYE_STARTS_WITHIN = 2.5  # if he says nothing by then, close anyway
GOODBYE_PAUSE = 0.4  # this much silence after he speaks means the goodbye is over
GOODBYE_AT_MOST = 10.0  # close by then whatever happens
GOODBYE_PLAYED_OUT = 0.15  # silence after his goodbye turn ends: the speaker has caught up
LISTEN_EVERY = 0.05


def check_every(idle_seconds: float) -> float:
    """How often Pipecat reports quiet: early enough to warn before closing."""
    return max(idle_seconds - WARNING_SECONDS, 1.0)


class QuietTimer:
    def __init__(self, worker, engine, warning_sound, idle_seconds: float, is_busy, on_closing):
        self._worker, self._engine, self._warning_sound = worker, engine, warning_sound
        self._interval = check_every(idle_seconds)
        self._is_busy, self._on_closing = is_busy, on_closing
        self._quiet_since: float | None = None
        self._closing_soon: asyncio.Task | None = None
        self._sleeping = False
        self._goodbye_said = False

    def listen(self, worker, user_turns) -> None:
        @worker.event_handler("on_idle_timeout")
        async def on_idle(_worker):
            await self.on_idle()

        @user_turns.event_handler("on_user_turn_started")
        async def on_user_started(_aggregator, _strategy):
            self.you_spoke()

    async def on_idle(self) -> None:
        """Pipecat calls this every `check_every` seconds without Alfred."""
        if self._closing_soon:
            return
        now = time.monotonic()
        self._quiet_since = self._quiet_since or now - self._interval
        quiet = now - self._quiet_since
        if self._is_busy() and quiet < KEEP_OPEN_SECONDS:
            return
        logger.info(f"Quiet for {quiet:.0f} s: warning tone, closing in {WARNING_SECONDS} s")
        self._engine.play(self._warning_sound)
        self._closing_soon = asyncio.create_task(self._close_after(WARNING_SECONDS))

    def activity(self) -> None:
        """Alfred spoke or got to work."""
        self._quiet_since = None
        if not self._sleeping:
            self._cancel()

    def you_spoke(self) -> None:
        """You spoke after the tone: stay, and count again."""
        if self._closing_soon and not self._sleeping:
            logger.info("Speech after the warning: staying open")
            self._quiet_since = None
            self._cancel()

    def sleep_after_goodbye(self) -> None:
        if self._sleeping:
            return
        logger.info("Going to sleep after the goodbye")
        self._cancel()
        self._sleeping = True
        self._closing_soon = asyncio.create_task(self._close_after_goodbye())

    @property
    def sleeping(self) -> bool:
        return self._sleeping

    def goodbye_said(self) -> None:
        """His turn after "go to sleep" ended: close once it has played out."""
        self._goodbye_said = True

    def stop(self) -> None:
        self._cancel()

    def _cancel(self) -> None:
        if self._closing_soon:
            self._closing_soon.cancel()
            self._closing_soon = None

    async def _close_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self._close()

    async def _close_after_goodbye(self) -> None:
        """Close once his goodbye has played out (his turn ended, or GOODBYE_PAUSE of
        silence after he spoke), or if he says nothing within GOODBYE_STARTS_WITHIN."""
        started = time.monotonic()
        last_heard: float | None = None
        while time.monotonic() - started < GOODBYE_AT_MOST:
            await asyncio.sleep(LISTEN_EVERY)
            now = time.monotonic()
            if self._engine.speaking:
                last_heard = now
            elif self._goodbye_said and now - (last_heard or started) >= GOODBYE_PLAYED_OUT:
                break
            elif last_heard is not None and now - last_heard >= GOODBYE_PAUSE:
                break
            elif last_heard is None and now - started > GOODBYE_STARTS_WITHIN:
                break
        await self._close(at_once=True)

    async def _close(self, at_once: bool = False) -> None:
        self._on_closing()
        if at_once:
            await self._worker.cancel(reason="told to go to sleep")
        else:
            await self._worker.queue_frame(EndFrame())
