"""Start Alfred: `uv run alfred`.

    listen for "Hey Alfred" -> chime -> talk (voice.py) -> quiet or "go to sleep" -> listen

The agent's tasks run alongside; a soft chime plays when one finishes. Alfred also speaks
up by himself when a task finishes or needs your answer, and when a reminder is due (a
timer or an alarm rings first, until you say "Hey Alfred" or a minute passes). After each
conversation, memory.py keeps what's worth remembering.
"""

import asyncio
import gc
import os
import signal
import sys
from importlib.metadata import version

from loguru import logger

from alfred import display
from alfred.agent import Agent
from alfred.audio import AudioEngine, load_sound
from alfred.config import PROJECT_ROOT, Config, ConfigError, load_config
from alfred.lock import only_one_alfred
from alfred.logs import setup_logging
from alfred.services import Services
from alfred.voice import run_conversation
from alfred.wakeword import WakeWordDetector

RING_SECONDS = 60


async def run(config: Config) -> None:
    detector = WakeWordDetector(config.wakeword_dir, config.wake_threshold)
    sounds = PROJECT_ROOT / "sounds"
    wake, done, offline, timer, alarm = (
        load_sound(sounds / f"{n}.wav")
        for n in ("listening_start", "task_done", "connection_error", "timer_done", "alarm")
    )
    engine = AudioEngine(config.input_device, config.output_device, config.echo_cancellation)
    engine.start()
    services = Services.start(config)
    show_library(services.library)
    claude = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    checks = {"mic": True, "OpenAI": bool(config.openai_api_key), "Claude": bool(claude)}
    display.card(version("alfred"), checks, services.web.ip_address if services.web else None)
    services.agent.on_finished.append(lambda _task: engine.play(done))
    handle_shutdown_signals(services.agent)
    background: set[asyncio.Task] = set()
    try:
        while True:
            logger.info('Waiting for "Hey Alfred"...')
            display.status(mode='Say "Hey Alfred"')
            woken = await wait_for_wake_or_news(engine, detector, services)
            logger.info(
                f"Wake word heard (score {detector.last_score:.2f})" if woken else "Speaking up"
            )
            if not woken and (due := services.reminders.new_due()):
                services.reminders.spoke_up_for(due)
                kinds = {r.kind for r in due}
                ringing = alarm if "alarm" in kinds else timer if "timer" in kinds else None
                if ringing:
                    woken = await ring(engine, detector, ringing)
            engine.play(wake if woken else done)
            display.status(mode="Listening")
            transcript = await run_conversation(engine, services, woken)
            if asyncio.current_task().cancelling():  # Pipecat swallows Ctrl+C: pass it on
                raise asyncio.CancelledError
            if transcript is None:
                engine.play(offline)  # two low beeps: couldn't reach OpenAI
            else:
                learning = asyncio.create_task(services.memory.learn(transcript))
                background.add(learning)
                learning.add_done_callback(background.discard)
            detector.reset()
            engine.clear_mic()  # the end of the conversation mustn't wake him again
    finally:
        for job in background:
            job.cancel()
        await services.agent.close()  # unfinished tasks carry on next start
        await services.apps.stop_all()
        await services.library.stop()
        if services.web:
            services.web.stop()
        services.db.close()
        engine.close()
        gc.collect()  # Claude's subprocess objects, while the loop can still close them
        await asyncio.sleep(0.1)


def show_library(library) -> None:
    """The status bar's count of files, kept up to date."""

    def count(*_) -> None:
        docs = library.documents()
        ready = sum(d.status == "ready" for d in docs)
        display.status(files=f"{ready} of {len(docs)} files ready" if docs else "no files")

    library.on_change.append(count)
    library.on_ready.append(count)
    count()


async def wait_for_wake_or_news(
    engine: AudioEngine, detector: WakeWordDetector, services: Services
) -> bool:
    """True for the wake word; False when a task has finished or needs an answer, or a
    reminder is due."""
    finished = asyncio.Event()

    def task_ended(_task) -> None:
        finished.set()

    services.agent.on_finished.append(task_ended)
    try:
        while True:
            finished.clear()
            heard = asyncio.create_task(wait_for_wake_word(engine, detector))
            waits = {
                heard,
                asyncio.create_task(services.questions.new_question.wait()),
                asyncio.create_task(finished.wait()),
                asyncio.create_task(services.reminders.wait_until_due()),
            }
            try:
                done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiting in waits:
                    waiting.cancel()
            if heard in done:
                return True
            services.questions.new_question.clear()
            if services.has_news():
                return False
    finally:
        services.agent.on_finished.remove(task_ended)


async def ring(engine: AudioEngine, detector: WakeWordDetector, sound: bytes) -> bool:
    """Ring until "Hey Alfred" (True) or RING_SECONDS pass (False)."""
    heard = asyncio.create_task(wait_for_wake_word(engine, detector))
    ends = asyncio.get_running_loop().time() + RING_SECONDS
    try:
        while not heard.done() and asyncio.get_running_loop().time() < ends:
            if engine.queued_playback_seconds() < 0.5:
                engine.play(sound)
            await asyncio.wait({heard}, timeout=0.25)
        return heard.done()
    finally:
        heard.cancel()
        engine.stop_playback()


async def wait_for_wake_word(engine: AudioEngine, detector: WakeWordDetector) -> None:
    while not detector.process(await engine.mic.get()):
        pass


def handle_shutdown_signals(agent: Agent) -> None:
    """Ctrl+C (or systemd's stop) ends Alfred cleanly; a second Ctrl+C at once."""
    main_task, stopping = asyncio.current_task(), False

    def shut_down() -> None:
        nonlocal stopping
        if stopping:
            os._exit(1)
        stopping = True
        logger.info("Shutting down (Ctrl+C again to force)...")
        agent.begin_shutdown()
        main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, shut_down)


def quiet_closed_loop_noise(unraisable) -> None:
    """Hide the harmless "Event loop is closed" left by subprocess objects at exit."""
    error = unraisable.exc_value
    if isinstance(error, RuntimeError) and str(error) == "Event loop is closed":
        return
    sys.__unraisablehook__(unraisable)


def main() -> None:
    os.environ.pop("VIRTUAL_ENV", None)  # Alfred's own environment confuses the agent's uv
    try:
        config = load_config()
    except ConfigError as error:
        sys.exit(f"Config error: {error}")
    _lock = only_one_alfred(config.data_dir)
    if _lock is None:
        sys.exit("Alfred is already running (stop the other one first).")
    display.on = "--plain" not in sys.argv and display.console.is_terminal
    setup_logging(config)
    sys.unraisablehook = quiet_closed_loop_noise
    display.start()
    try:
        asyncio.run(run(config))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Bye.")
    except Exception:
        logger.exception("Alfred crashed")
        sys.exit(1)
    finally:
        display.stop()


if __name__ == "__main__":
    main()
