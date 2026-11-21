"""One spoken conversation with GPT-Live, from the wake word to quiet or "go to sleep".

    microphone -> GPT-Live -> speaker

GPT-Live listens and talks at once, so you can interrupt it. It can't call tools itself:
it delegates to a text model (the delegate) that runs web searches on OpenAI's side and
calls Alfred's tools here (tools.py), above all do_task, which hands work to the agent.
"""

import asyncio
import re
import time

from loguru import logger
from pipecat.adapters.llm.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.frames.frames import (
    BotSpeakingFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMRunFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.loggers.debug_log_observer import DebugLogObserver
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.openai.live.llm import OpenAILiveLLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.workers.runner import WorkerRunner

from alfred import display
from alfred.announcer import Announcer, opening_messages, recent_context
from alfred.audio import SAMPLE_RATE, AudioEngine, load_sound
from alfred.config import PROJECT_ROOT, Config, load_prompt
from alfred.idle import QuietTimer, check_every
from alfred.services import Services
from alfred.tools import delegate_tools, not_set_up
from alfred.transport import EngineTransport

LOGGED_FRAMES = (  # worth a line in the debug log
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    TranscriptionFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    ErrorFrame,
)
WEB_SEARCH = {"type": "web_search"}  # hosted by OpenAI
VOICE_PER_MINUTE = 0.05  # GPT-Live's price in dollars, billed per second
LOOKUP_SECONDS = 30  # a delegation gets this long before quiet counts
GREET_AFTER = 1.0  # no speech this long after the session opens: just the wake word
GREETING = 'The user only said your name. Greet them in two or three words ("Yes, sir?").'
RINGS = {"alarm": "alarm", "timer": "timer_done"}  # sounds for reminders due mid-conversation
NO_CREDITS = ("no credits remaining", "insufficient_quota", "exceeded your current quota")


async def run_conversation(engine: AudioEngine, services: Services, woken: bool = False):
    """Talk until it goes quiet. Returns what was said, line by line, or None if the
    session never opened. woken: by "Hey Alfred" (he may greet you)."""
    return await Conversation(engine, services, woken).run()


class Conversation:
    def __init__(self, engine: AudioEngine, services: Services, woken: bool):
        s = self.services = services
        self.engine, self._woken = engine, woken
        self.transcript: list[str] = []
        self._user_spoke = asyncio.Event()
        self._looked_up_at = -1e9
        self.llm = create_gpt_live(s.config, *background(s))
        opening, self._told, self._said = opening_messages(s.db, s.questions, s.reminders, s.health)
        self._has_news = bool(opening)
        tools = delegate_tools(
            s.agent,
            s.db,
            s.questions,
            s.memory,
            s.reminders,
            s.apps,
            library=s.library,
            health=s.health,
            music=s.music,
            google=s.google,
        )
        context = LLMContext(
            messages=opening,
            tools=ToolsSchema(
                standard_tools=tools, custom_tools={AdapterType.OPENAI: [WEB_SEARCH]}
            ),
        )
        self.transport = EngineTransport(engine)
        user_turns, alfred_turns = LLMContextAggregatorPair(context)
        self.worker = create_worker(
            Pipeline(
                [
                    self.transport.input(),
                    user_turns,
                    self.llm,
                    self.transport.output(),
                    alfred_turns,
                ]
            ),
            s.config,
        )
        self.announcer = Announcer(
            self.llm,
            s.db,
            s.agent,
            s.questions,
            s.reminders,
            ring=self._ring,
            library=s.library,
            health=s.health,
        )
        self._rings = {
            k: load_sound(PROJECT_ROOT / "sounds" / f"{f}.wav") for k, f in RINGS.items()
        }
        self._sleep_sound = load_sound(PROJECT_ROOT / "sounds" / "session_end.wav")
        self._sleep_sound_played = False
        self.timer = QuietTimer(
            self.worker,
            engine,
            load_sound(PROJECT_ROOT / "sounds" / "session_warning.wav"),
            s.config.idle_seconds,
            is_busy=lambda: s.agent.busy or bool(s.questions.waiting()) or self._looking_up(),
            on_closing=self._closing,
        )
        self.timer.listen(self.worker, user_turns)
        self.out_of_credits = end_if_out_of_credits(self.worker)
        user_turns.add_event_handler("on_user_turn_started", self._user_started)
        self.llm.add_event_handler("on_session_started", self._session_started)
        log_conversation(self.llm, user_turns, alfred_turns, self._line, self._delegated)

    async def run(self) -> list[str] | None:
        self.services.questions.new_question.clear()  # what's waiting is in the opening
        self.announcer.start()
        await self.worker.queue_frame(LLMRunFrame())  # opens the GPT-Live session
        runner = WorkerRunner(handle_sigint=False)  # Ctrl+C is handled in __main__.py
        await runner.add_workers(self.worker)
        started = time.monotonic()
        try:
            await runner.run()
        finally:
            self.announcer.stop()
            self.timer.stop()
            minutes = (time.monotonic() - started) / 60
            display.status(mode='Say "Hey Alfred"', cost=minutes * VOICE_PER_MINUTE)
            logger.info(
                f"Session closed after {minutes:.1f} min (~${minutes * VOICE_PER_MINUTE:.2f})"
            )
        if self.out_of_credits.is_set() or not self.announcer.session_open:
            if not self.out_of_credits.is_set():
                logger.warning("Couldn't open a GPT-Live session: check the internet connection")
            return None
        if not asyncio.current_task().cancelling():
            self._play_sleep_sound()
        return self.transcript

    def _ring(self, due) -> None:
        for kind in ("alarm", "timer"):
            if any(r.kind == kind for r in due):
                self.engine.play(self._rings[kind])
                return

    def _closing(self) -> None:
        self.announcer.closing = True
        self._play_sleep_sound()

    def _play_sleep_sound(self) -> None:
        if not self._sleep_sound_played:
            self._sleep_sound_played = True
            self.engine.play(self._sleep_sound)

    async def _session_started(self, _llm, _session) -> None:
        self.announcer.session_open = True
        self.services.db.mark_told(self._told)
        self.services.reminders.mark_said(self._said)
        logger.info("Listening (GPT-Live session open)")
        self.transport.mic_input.start_forwarding()
        if self._woken and not self._has_news:
            self.llm.create_task(self._greet_unless_asked(), "greet")

    async def _greet_unless_asked(self) -> None:
        """'Hey Alfred' alone gets a greeting; 'Hey Alfred, what time is it?' an answer."""
        try:
            await asyncio.wait_for(self._user_spoke.wait(), GREET_AFTER)
        except TimeoutError:
            self.announcer.tell(GREETING)

    async def _user_started(self, _aggregator, _strategy) -> None:
        self._user_spoke.set()

    def _line(self, who: str, text: str) -> None:
        self.transcript.append(f"{who}: {text}")
        if who == "Alfred":
            self.timer.activity()
            if is_goodbye(text):
                self.timer.sleep_after_goodbye()
            if self.timer.sleeping and text:  # the goodbye: say nothing after it
                self.transport.output().muted = True
                self.timer.goodbye_said()

    def _delegated(self) -> None:
        self._looked_up_at = time.monotonic()
        self.timer.activity()

    def _looking_up(self) -> bool:
        return time.monotonic() - self._looked_up_at < LOOKUP_SECONDS


def background(services: Services) -> tuple[str, str]:
    """Added to GPT-Live's and the delegate's instructions: the time and place, what
    Alfred knows about the user, the files in his library, recent tasks, projects and the
    apps page."""
    s = services
    shared = s.config.context_text() + s.memory.prompt_text() + s.library.contents_text()
    projects = ", ".join(s.agent.projects.names()) or "none yet"
    delegate = shared + f"\n\n## Projects\n{projects}"
    delegate += not_set_up(s.music, s.google)
    if s.web:
        delegate += s.web.address_text()
    return shared + recent_context(s.db), delegate + recent_context(s.db)


def create_gpt_live(
    config: Config, voice_extra: str = "", delegate_extra: str = ""
) -> OpenAILiveLLMService:
    logger.debug(f"Opening GPT-Live: {config.live_model}, delegate {config.delegate_model}")
    return OpenAILiveLLMService(
        api_key=config.openai_api_key,
        settings=OpenAILiveLLMService.Settings(
            model=config.live_model,
            system_instruction=load_prompt("voice") + voice_extra,
            voice=config.voice,
        ),
        delegation=OpenAILiveLLMService.ResponsesDelegation(
            settings=OpenAIResponsesLLMService.Settings(
                model=config.delegate_model,
                system_instruction=load_prompt("delegate") + delegate_extra,
                reasoning=OpenAIResponsesLLMService.ReasoningConfig(effort="low"),
            ),
        ),
    )


def create_worker(pipeline: Pipeline, config: Config) -> PipelineWorker:
    return PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        idle_timeout_secs=check_every(config.idle_seconds),
        idle_timeout_frames=(BotSpeakingFrame, FunctionCallInProgressFrame),  # Alfred, not noise
        cancel_on_idle_timeout=False,  # idle.py warns, then closes
        processor_unusable_policy=ProcessorUnusablePolicy.END,  # a dropped connection ends it
        observers=[DebugLogObserver(frame_types=LOGGED_FRAMES), MetricsLogObserver()],
    )


def end_if_out_of_credits(worker) -> asyncio.Event:
    """With no OpenAI credits nothing works: say so and end at once, not sit silent."""
    out = asyncio.Event()

    @worker.event_handler("on_pipeline_error")
    async def on_error(_worker, frame):
        if out.is_set() or not any(words in str(frame.error).lower() for words in NO_CREDITS):
            return
        out.set()
        logger.error(
            "OpenAI credits used up: https://platform.openai.com/settings/organization/billing"
        )
        out.cancelling = asyncio.create_task(worker.cancel(reason="no OpenAI credits"))

    return out


def is_goodbye(text: str) -> bool:
    """Alfred ends a conversation by saying "Goodnight, sir." (prompts/voice.md), so it
    closes the moment that's played, with no round trip through the delegate."""
    return re.sub(r"[^a-z ]", "", text.lower()).strip().endswith("goodnight sir")


def log_conversation(llm, user_turns, alfred_turns, on_line, on_delegation=None) -> None:
    """Log what was said and each delegation. on_line(who, text) gets every line."""

    @llm.event_handler("on_delegation_created")
    async def on_delegation_created(_llm, delegation):
        logger.info(f"Delegating ({delegation.id})")
        if on_delegation:
            on_delegation()

    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(_llm, calls):
        logger.info(f"Using: {', '.join(call.function_name for call in calls)}")
        if on_delegation:
            on_delegation()

    @user_turns.event_handler("on_user_turn_message_added")
    async def on_user_turn(_aggregator, message):
        if text := spoken(message.content):
            logger.info(f"You: {text}")
            display.said("You", text)
            on_line("You", text)

    @alfred_turns.event_handler("on_assistant_turn_stopped")
    async def on_alfred_turn(_aggregator, message):
        text = spoken(message.content)
        if text:
            logger.info(f"Alfred: {text}")
            display.said("Alfred", text)
        on_line("Alfred", text)


def spoken(content) -> str:
    """The words of a turn, without the sounds GPT-Live notes: "[breath]", "[laughs]"."""
    return " ".join(re.sub(r"\[[^\]]*\]", " ", str(content or "")).split())
