"""Microphone and speaker, with optional WebRTC echo cancellation.

Without an echo-cancelling microphone, Alfred would hear its own voice and
interrupt itself. We use the WebRTC echo canceller from the `livekit` package
as a plain library (no LiveKit server). It can be switched off with
ALFRED_ECHO_CANCELLATION=off to compare; the mic audio is then passed on raw.

One full-duplex sounddevice stream runs both directions in the same 10 ms
callback. Each call:
  1. takes the next 10 ms of speaker audio and plays it,
  2. gives that same audio to the echo canceller as the "far end",
  3. cleans the 10 ms the microphone just captured,
  4. hands the clean audio to asyncio through the `mic` queue.
Doing both sides in one callback keeps them in step, which echo cancellation needs.

Everything runs at 16 kHz: the wake word needs it, and Pipecat resamples to
whatever rate GPT-Live uses.
"""

import asyncio
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from livekit import rtc
from loguru import logger

SAMPLE_RATE = 16_000
BLOCK_SAMPLES = 160  # 10 ms, the frame size WebRTC processing requires
MIC_QUEUE_BLOCKS = 500  # 5 s; holds what you say while a session is still connecting
# Ask the device for small buffers: every 10 ms of input buffering delays barge-in.
# The device may pick more; the actual value is logged at startup.
REQUESTED_LATENCY_SECONDS = 0.02
AUDIBLE = 100  # a sample louder than this (about -50 dBFS) is sound, not silence


class AudioEngine:
    def __init__(
        self,
        input_device: int | str | None = None,
        output_device: int | str | None = None,
        echo_cancellation: bool = True,
    ):
        self.mic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_BLOCKS)
        self._speaker = bytearray()  # audio waiting to be played
        self._speaker_lock = threading.Lock()  # shared by asyncio and the audio thread
        self._echo_canceller = (
            rtc.AudioProcessingModule(
                echo_cancellation=True,
                noise_suppression=True,
                high_pass_filter=True,
                auto_gain_control=True,
            )
            if echo_cancellation
            else None
        )
        self._stream = sd.Stream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SAMPLES,
            channels=1,
            dtype="int16",
            latency=REQUESTED_LATENCY_SECONDS,
            device=(input_device, output_device),
            callback=self._on_audio,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._delay_ms = 0
        self._reported: set[str] = set()  # audio problems already in the log
        self._sound_at = 0.0  # when the speaker last played sound (not silence)

    def start(self) -> None:
        """Start the stream. Call from inside the asyncio event loop."""
        self._loop = asyncio.get_running_loop()
        self._stream.start()
        input_latency, output_latency = self._stream.latency
        # Time from audio leaving us to its echo coming back: the canceller needs this.
        self._delay_ms = round((input_latency + output_latency) * 1000)
        mic, speaker = (sd.query_devices(i) for i in self._stream.device)
        logger.info(
            f"Audio started: mic '{mic['name']}', speaker '{speaker['name']}', {SAMPLE_RATE} Hz, "
            f"echo cancellation {'ON' if self._echo_canceller else 'OFF'}"
        )
        # If a device's own rate differs from 16 kHz, it converts, which can add delay.
        logger.debug(
            f"Latency: input {input_latency * 1000:.0f} ms, output {output_latency * 1000:.0f} ms "
            f"(asked for {REQUESTED_LATENCY_SECONDS * 1000:.0f} ms; echo delay estimate "
            f"{self._delay_ms} ms). Device rates: mic {mic['default_samplerate']:.0f} Hz, "
            f"speaker {speaker['default_samplerate']:.0f} Hz"
        )

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()

    def play(self, pcm: bytes) -> None:
        """Queue 16 kHz 16-bit mono audio for the speaker."""
        with self._speaker_lock:
            self._speaker.extend(pcm)

    def stop_playback(self) -> None:
        with self._speaker_lock:
            self._speaker.clear()

    @property
    def speaking(self) -> bool:
        """Is the speaker playing sound now? Silence doesn't count: Pipecat pads Alfred's
        voice with it."""
        return time.monotonic() - self._sound_at < 0.05

    def queued_playback_seconds(self) -> float:
        with self._speaker_lock:
            return len(self._speaker) / 2 / SAMPLE_RATE

    def clear_mic(self) -> None:
        """Drop microphone audio nobody has read yet."""
        while not self.mic.empty():
            self.mic.get_nowait()

    # --- runs on the audio thread: keep it short and never block ---

    def _on_audio(
        self, indata: np.ndarray, outdata: np.ndarray, frames: int, _time, status
    ) -> None:
        try:
            speaker = self._take_speaker_audio(frames * 2)
            samples = np.frombuffer(speaker, dtype=np.int16)
            outdata[:, 0] = samples
            if np.abs(samples).max() > AUDIBLE:
                self._sound_at = time.monotonic()
            mic = indata.tobytes()

            if self._echo_canceller:
                far = rtc.AudioFrame(bytearray(speaker), SAMPLE_RATE, 1, frames)
                near = rtc.AudioFrame(bytearray(mic), SAMPLE_RATE, 1, frames)
                self._echo_canceller.process_reverse_stream(far)
                self._echo_canceller.set_stream_delay_ms(self._delay_ms)
                self._echo_canceller.process_stream(near)  # cleans `near` in place
                mic = bytes(near.data)

            device_status = str(status) if status else None  # e.g. "input overflow"
            self._loop.call_soon_threadsafe(self._deliver, mic, device_status)
        except Exception as error:  # an exception here would silently kill the stream
            outdata.fill(0)
            self._loop.call_soon_threadsafe(self._report, f"audio callback failed: {error!r}")

    def _take_speaker_audio(self, num_bytes: int) -> bytes:
        with self._speaker_lock:
            chunk = bytes(self._speaker[:num_bytes])
            del self._speaker[:num_bytes]
        return chunk.ljust(num_bytes, b"\x00")  # silence when Alfred isn't talking

    # --- back on the asyncio thread ---

    def _deliver(self, mic: bytes, device_status: str | None) -> None:
        if self.mic.full():
            self.mic.get_nowait()  # nobody is reading: drop the oldest 10 ms
        self.mic.put_nowait(mic)
        if device_status:
            self._report(f"audio device: {device_status}")

    def _report(self, problem: str) -> None:
        """Each kind of problem once (an overflow can repeat 100 times a second)."""
        if problem not in self._reported:
            self._reported.add(problem)
            logger.warning(f"{problem} (further ones aren't logged)")


def load_sound(path: Path) -> bytes:
    """Read a WAV file for `AudioEngine.play`. It must already be 16 kHz, mono, 16-bit."""
    with wave.open(str(path)) as sound:
        shape = (sound.getframerate(), sound.getnchannels(), sound.getsampwidth())
        if shape != (SAMPLE_RATE, 1, 2):
            raise ValueError(f"{path.name} must be 16 kHz mono 16-bit, got {shape}")
        return sound.readframes(sound.getnframes())
