"""Benchmark mode stays "almost exactly" Alfred: the same prompts minus the switched-off
features, and the same GPT-Live settings."""

import sys

import pytest

from alfred.config import PROJECT_ROOT, Config, load_prompt

sys.path.insert(0, str(PROJECT_ROOT))  # benchmark/ isn't part of the installed package

from benchmark.prompts import THIS_SESSION, benchmark_prompt  # noqa: E402


@pytest.mark.parametrize("name", ["voice", "delegate"])
def test_benchmark_prompts_are_alfreds_plus_a_note(name):
    assert benchmark_prompt(name) == load_prompt(name) + THIS_SESSION


def test_benchmark_entry_uses_16_khz():
    from benchmark.assistant_entry import PROJECT_MARKER, audio_rates

    assert audio_rates() == (16000, 16000)
    assert (PROJECT_ROOT / PROJECT_MARKER).is_file()


def test_benchmark_gpt_live_is_built_by_alfreds_own_code():
    pytest.importorskip("pipecat.services.openai.live.llm")
    import alfred.voice as alfred_voice
    from benchmark.voice import create_gpt_live

    built = []
    real_create = alfred_voice.create_gpt_live

    def spy(config, voice_extra, delegate_extra):
        built.append((alfred_voice.load_prompt("delegate"), voice_extra))
        return real_create(config, voice_extra, delegate_extra)

    alfred_voice.create_gpt_live = spy
    try:
        create_gpt_live(Config(openai_api_key="sk-test"))
    finally:
        alfred_voice.create_gpt_live = real_create
    delegate_prompt, extra = built[0]
    assert delegate_prompt.endswith(THIS_SESSION.strip())
    assert "Manila" in extra
    assert alfred_voice.load_prompt is load_prompt  # put back afterwards


def test_first_audio_of_each_reply_is_marked_once(monkeypatch):
    """GPT-Live's audio frames aren't a TTS service's, so fd-benchmark's stage marks miss
    "TTS first audio" without this."""
    import asyncio

    from pipecat.frames.frames import SpeechOutputAudioRawFrame, TTSStartedFrame
    from pipecat.processors.frame_processor import FrameProcessor

    from benchmark.voice import FirstAudioMark

    async def passed_on(self, frame, direction=None):
        pass

    monkeypatch.setattr(FrameProcessor, "process_frame", passed_on)  # no pipeline needed
    monkeypatch.setattr(FrameProcessor, "push_frame", passed_on)
    marks = []
    step = FirstAudioMark()
    step._mark = marks.append
    audio = SpeechOutputAudioRawFrame(audio=b"\0\0", sample_rate=16000, num_channels=1)

    async def main():
        for frame in (audio, TTSStartedFrame(), audio, audio, TTSStartedFrame(), audio):
            await step.process_frame(frame, None)

    asyncio.run(main())
    assert marks == ["tts_first_audio", "tts_first_audio"]  # one per reply, none before
