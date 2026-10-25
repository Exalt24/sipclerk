import numpy as np

from sipclerk import audio
from sipclerk.sip.phone import tone


def _speechlike(seconds: float = 1.0) -> bytes:
    t = np.arange(int(8000 * seconds)) / 8000
    x = 5000 * np.sin(2 * np.pi * 300 * t) + 3000 * np.sin(2 * np.pi * 1100 * t) + 1500 * np.sin(2 * np.pi * 2900 * t)
    return x.astype(np.int16).tobytes()


def test_stream_upsampler_matches_a_whole_signal_resample_apart_from_its_two_ms_delay():
    pcm = _speechlike()
    whole = np.frombuffer(audio.resample(pcm, 8000, 16000), dtype=np.int16).astype(np.float64)
    up = audio.StreamUpsampler()
    streamed = np.frombuffer(b"".join(up.feed(f) for f in audio.frames(pcm)), dtype=np.int16).astype(np.float64)
    assert len(streamed) == len(whole)
    # the streamed version lags by RIGHT input samples (2 per output); compare after aligning, skipping the first frame
    lag = 2 * audio.StreamUpsampler.RIGHT
    a, b = streamed[lag + 640:], whole[640:len(streamed) - lag]
    err = np.sqrt(np.mean((a[:len(b)] - b[:len(a)]) ** 2)) / np.sqrt(np.mean(b ** 2))
    assert err < 0.02, err


def test_stream_upsampler_has_no_seams_at_frame_edges():
    pcm = tone(500, 0.5, amp=8000)
    up = audio.StreamUpsampler()
    out = np.frombuffer(b"".join(up.feed(f) for f in audio.frames(pcm)), dtype=np.int16).astype(np.float64)
    step = np.abs(np.diff(out))
    edges = step[319::320]  # the sample step across each 20 ms boundary
    assert edges.max() < 1.5 * step[1000:].max() + 1  # no click larger than the tone's own steepest slope


def test_stream_upsampler_keeps_digital_silence_silent():
    up = audio.StreamUpsampler()
    out = b"".join(up.feed(bytes(320)) for _ in range(100))
    assert audio.rms(out) == 0 and len(out) == 100 * 640


def test_frames_pad_the_last_frame_with_silence():
    fr = audio.frames(bytes(320 + 10))
    assert len(fr) == 2 and all(len(f) == 320 for f in fr)
