from alfred.audio import SAMPLE_RATE, load_sound
from alfred.config import PROJECT_ROOT


def test_wake_sound_is_in_engine_format():
    audio = load_sound(PROJECT_ROOT / "sounds" / "listening_start.wav")
    seconds = len(audio) / 2 / SAMPLE_RATE
    assert 0.2 < seconds < 1.5  # short enough not to delay the conversation


def test_every_sound_is_in_engine_format():
    for wav in (PROJECT_ROOT / "sounds").glob("*.wav"):
        assert load_sound(wav)  # raises if it isn't 16 kHz mono 16-bit


def test_silence_isnt_speaking(monkeypatch):
    """Pipecat pads Alfred's voice with silence: only sound counts as speaking."""
    import numpy as np

    from alfred import audio

    engine = audio.AudioEngine.__new__(audio.AudioEngine)  # no sound device needed
    engine._speaker, engine._speaker_lock = bytearray(), audio.threading.Lock()
    engine._echo_canceller, engine._sound_at = None, 0.0
    engine._loop = type("Loop", (), {"call_soon_threadsafe": lambda *a: None})()
    out = np.zeros((160, 1), dtype=np.int16)
    mic = np.zeros((160, 1), dtype=np.int16)
    engine.play(bytes(320))  # 10 ms of silence
    engine._on_audio(mic, out, 160, None, None)
    assert not engine.speaking
    engine.play(np.full(160, 3000, dtype=np.int16).tobytes())  # 10 ms of sound
    engine._on_audio(mic, out, 160, None, None)
    assert engine.speaking
