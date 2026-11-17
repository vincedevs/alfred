# Benchmarking Alfred's voice

[fd-benchmark](https://github.com/trueshotjames/ai231-me3-full-duplex-benchmarking) plays
test clips straight into Alfred, with no microphone or speaker, and scores turn-taking,
pauses, interruptions, false turn-taking and latency. Alfred runs his own voice pipeline
(GPT-Live and the delegate with web search) in benchmark mode:

| | Alfred | Benchmark mode |
|---|---|---|
| GPT-Live, voice, delegate, web search, settings from `alfred/.env` | ✓ | same |
| Voice and delegate prompts | his own | his own, plus a note that only web search is on |
| Wake word, sounds, closing when quiet | ✓ | off: one session per round |
| The agent, memory, skills, library | ✓ | off: nothing is written to `~/.alfred` |
| Microphone, speaker, echo cancellation | ✓ | bypassed: the benchmark sends audio directly |

Nothing in Alfred's repository is needed for it except Alfred himself: the one file that
connects the two lives inside fd-benchmark (below).

## Repositories

| Repository | What it is | Where it goes |
|---|---|---|
| [vincedevs/alfred](https://github.com/vincedevs/alfred) | Alfred | your `alfred` folder |
| [trueshotjames/ai231-me3-full-duplex-benchmarking](https://github.com/trueshotjames/ai231-me3-full-duplex-benchmarking) | the benchmark | inside the `alfred` folder, as `fd-benchmark/` (already in Alfred's `.gitignore`) |

## Set up (once)

1. Alfred works first: `uv sync` in the `alfred` folder, and `alfred/.env` has your
   `OPENAI_API_KEY`.

2. Clone and install fd-benchmark inside the `alfred` folder:

   ```bash
   cd ~/Documents/projects/alfred
   git clone https://github.com/trueshotjames/ai231-me3-full-duplex-benchmarking.git fd-benchmark
   cd fd-benchmark
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt -r requirements-score.txt
   .venv/bin/python tools/fetch_datasets.py    # "datasets ready: 430 audio files in 9 groups"
   cp .env.example .env                        # a judge key here is optional
   ```

3. Save the file below as `fd-benchmark/assistant_entry.py`. fd-benchmark's launcher
   picks it up there and finds Alfred in the folder above (fd-benchmark's git ignores the
   file, so it stays yours).

4. Check it, from `fd-benchmark`, with Alfred's Python. The last line must say `READY`:

   ```bash
   ../.venv/bin/python launch_assistant.py --check
   ```

## Run

From `fd-benchmark`, one round (about 7 minutes, roughly $0.35 of GPT-Live):

```bash
.venv/bin/python -m fdbench run --student YOUR_ID --rounds 1 --connect direct --where local \
  --judge-provider none --yes --recordings k
```

Or answer its questions instead: `.venv/bin/python -m fdbench`. Start with one round.

## Results

- `fd-benchmark/runs/<student>/<date-time>/`: `results.html` (open it in a browser),
  `results.json`, `results.csv`, `summary.md`, `latency_chart.svg`, and every clip's
  recording (left: the test audio, right: Alfred).
- Alfred's own log of the run: `fd-benchmark/runs/_assistant/alfred-bench-<date-time>.log`.
- Compare runs: `.venv/bin/python -m fdbench compare runs/*`.

## Good to know

- **Interruptions:** GPT-Live handles being interrupted on OpenAI's side, and Pipecat
  doesn't send its usual "interrupted" signal, which the benchmark uses to stop counting
  audio already sent. Read the first round's interruption results with that in mind.
- **Background noise:** in normal use the wake word filters it; here every clip reaches
  Alfred, so "false turn-taking" scores are stricter than real life.
- **Stage timings:** fd-benchmark picks up Pipecat's stages by itself; the file adds the
  one it can't see, GPT-Live's first audio of each reply ("TTS first audio").
- **On the Pi:** fd-benchmark's [docs/GUIDE.md](https://github.com/trueshotjames/ai231-me3-full-duplex-benchmarking/blob/main/docs/GUIDE.md)
  covers running the benchmark on a laptop and Alfred on the Pi (steps 8–12).
- **If Alfred's code changes** the functions this file uses (`alfred.voice`:
  `create_gpt_live`, `create_worker`, `log_conversation`, `WEB_SEARCH`, `load_prompt`), run
  `--check` and one round again.

## The file: `fd-benchmark/assistant_entry.py`

```python
"""fd-benchmark's entry for Alfred: save as fd-benchmark/assistant_entry.py.

Runs Alfred's own voice pipeline (GPT-Live + the delegate with web search) on
fd-benchmark's transport. No wake word, no idle close, no agent, memory or skills, and
nothing is written to ~/.alfred. Alfred's keys and models come from alfred/.env.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

PROJECT_MARKER = "alfred/voice.py"  # how the launcher recognises Alfred's folder
ONE_HOUR = 3600.0  # Pipecat's idle time: longer than a round, so it never fires
THIS_SESSION = (
    "\n\n## In this session\nYou have web search only: the agent, memory and skills are "
    "off. If the user asks for one, say briefly that it isn't available right now."
)


def audio_rates() -> tuple[int, int]:
    return 16000, 16000  # Alfred runs at 16 kHz both ways (alfred/audio.py)


def describe() -> dict:
    from alfred.config import load_config

    config = load_config()
    speech = f"{config.live_model} (speech to speech)"
    return {
        "ASR": speech,
        "LLM": f"{speech}; delegate {config.delegate_model} for look-ups",
        "TTS": speech,
        "VAD": f"{config.live_model} turn detection",
    }


async def build(transport, log_dir: Path):
    from loguru import logger
    from pipecat.adapters.llm.schemas.tools_schema import AdapterType, ToolsSchema
    from pipecat.frames.frames import LLMRunFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
    from pipecat.workers.runner import WorkerRunner

    import alfred.voice as voice
    from alfred.config import ConfigError, load_config, load_prompt

    try:
        config = load_config()
    except ConfigError as error:
        sys.exit(f"Alfred's config: {error}")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"alfred-bench-{datetime.now():%Y%m%d-%H%M%S}.log"
    logger.remove()
    logger.add(sys.stderr, level=config.log_level)
    logger.add(log_file, level="DEBUG", backtrace=True, diagnose=False)

    # Alfred's own GPT-Live, with his prompts plus the note that only web search is on.
    voice.load_prompt = lambda name: load_prompt(name) + THIS_SESSION
    try:
        llm = voice.create_gpt_live(config, config.context_text(), config.context_text())
    finally:
        voice.load_prompt = load_prompt
    tools = ToolsSchema(standard_tools=[], custom_tools={AdapterType.OPENAI: [voice.WEB_SEARCH]})
    user_turns, alfred_turns = LLMContextAggregatorPair(LLMContext(messages=[], tools=tools))
    pipeline = Pipeline(
        [transport.input(), user_turns, llm, first_audio_mark(), transport.output(), alfred_turns]
    )
    worker = voice.create_worker(pipeline, replace(config, idle_seconds=ONE_HOUR))
    voice.log_conversation(llm, user_turns, alfred_turns, on_line=lambda _who, _text: None)
    await worker.queue_frame(LLMRunFrame())  # opens the GPT-Live session
    runner = WorkerRunner(handle_sigint=False)  # the launcher handles Ctrl+C
    logger.info(f"Alfred in benchmark mode; logging to {log_file}")

    async def run() -> None:
        await runner.add_workers(worker)
        await runner.run()

    async def cancel() -> None:
        await worker.cancel()

    return run(), cancel


def first_audio_mark():
    """Marks each reply's first audio for fd-benchmark's "TTS first audio" stage: GPT-Live's
    audio frames aren't a TTS service's, so the stage would show "not reported" without it."""
    from fdbench_marks import mark
    from pipecat.frames.frames import SpeechOutputAudioRawFrame, TTSStartedFrame
    from pipecat.processors.frame_processor import FrameProcessor

    class FirstAudioMark(FrameProcessor):
        def __init__(self):
            super().__init__()
            self._reply_started = False

        async def process_frame(self, frame, direction) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TTSStartedFrame):
                self._reply_started = True
            elif isinstance(frame, SpeechOutputAudioRawFrame) and self._reply_started:
                self._reply_started = False
                mark("tts_first_audio")
            await self.push_frame(frame, direction)

    return FirstAudioMark()
```

## Not yet: the documents benchmark

A benchmark for the library (answers and citations over your own files) is planned for
later; see the documents plan.
