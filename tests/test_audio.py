"""Audio features, onset/beat detection, sections and calibration - pure numpy, no lights.  SIMULATOR VERIFIED
(synthetic audio only; behaviour on real music through a real microphone is a hardware/field check)."""
import numpy as np

from audio.analyzer import MusicAnalyzer, build_calibration
from audio.features import FeatureExtractor
from audio.sources import SyntheticSource


def run_source(src, seconds, *, sensitivity=60, noise=0.002, seed=0, cal=None):
    fx = FeatureExtractor(src.sr, src.hop)
    an = MusicAnalyzer(fx.fps, sensitivity, cal)
    rng = np.random.default_rng(seed)
    loop = src._loop
    events, frames = [], []
    for n in range(int(seconds * src.sr / src.hop)):
        i = (n * src.hop) % len(loop)
        blk = loop[i:i + src.hop]
        if blk.size < src.hop:
            blk = np.concatenate([blk, loop[:src.hop - blk.size]])
        blk = blk + rng.standard_normal(src.hop).astype(np.float32) * noise
        f = fx.process(blk, n * src.hop / src.sr)
        frames.append(f)
        events.extend(an.process(f))
    return an, events, frames


def tone(freq, amp, sr=22050, hop=512, n=200):
    t = np.arange(hop * n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32).reshape(n, hop)


def test_features_identify_band_rms_and_centroid():
    fx = FeatureExtractor(22050, 512)
    last = None
    for b in tone(60, 0.5):
        last = fx.process(b, 0)
    assert last.bass > 10 * max(last.mid, last.treble)
    assert abs(last.rms - 0.5 / 2 ** 0.5) < 0.02
    fx = FeatureExtractor(22050, 512)
    for b in tone(3000, 0.3):
        last = fx.process(b, 0)
    assert last.treble > 10 * max(last.bass, last.mid)
    assert 2500 < last.centroid < 3500


def test_features_tolerate_odd_block_sizes():
    fx = FeatureExtractor(22050, 512)
    f = fx.process(np.zeros(300, np.float32), 0)
    assert f.rms == 0.0
    f = fx.process(np.ones(900, np.float32) * 0.1, 0.02)
    assert f.rms > 0


def test_beat_detection_accuracy_on_the_drop():
    src = SyntheticSource(realtime=False)
    an, events, _ = run_source(src, 120)
    beats = [e.t for e in events if e.kind == "beat" and not e.meta.get("predicted")]
    dstart, dend = src.section_starts["drop"], src.section_starts["breakdown"]
    hits = total = in_win = good = 0
    for c in range(int(120 // src.duration) + 1):
        a, b = c * src.duration + dstart + 4, c * src.duration + dend - 0.2     # after the tempo has locked
        if b > 120:
            break
        kicks = [k for k in src.kick_times if a <= c * src.duration + k <= b]
        kicks = [c * src.duration + k for k in kicks]
        for k in kicks:
            total += 1
            hits += any(-0.05 <= bt - k <= 0.12 for bt in beats)
        bb = [x for x in beats if a <= x <= b]
        in_win += len(bb)
        good += sum(any(-0.05 <= x - k <= 0.12 for k in kicks) for x in bb)
    assert total > 50
    assert hits / total >= 0.75, hits / total          # recall
    assert good / in_win >= 0.75, good / in_win        # precision
    assert abs(an.bpm - 124) <= 3, an.bpm


def test_beats_respect_a_cooldown():
    src = SyntheticSource(realtime=False)
    _, events, _ = run_source(src, 90)
    t = [e.t for e in events if e.kind == "beat" and not e.meta.get("predicted")]
    gaps = np.diff(t)
    assert gaps.min() >= 0.18, gaps.min()


def test_sections_build_drop_breakdown_are_found_near_the_truth():
    src = SyntheticSource(realtime=False)
    an, events, _ = run_source(src, 110)
    for c in range(3):
        base = c * src.duration
        d_true = base + src.section_starts["drop"]
        drops = [e.t for e in events if e.kind == "drop" and d_true - 1.5 <= e.t <= d_true + 3]
        assert drops, f"no drop near {d_true:.1f}: {[round(e.t, 1) for e in events if e.kind == 'drop']}"
        b_true = base + src.section_starts["breakdown"]
        assert any(b_true <= e.t <= b_true + 5 for e in events if e.kind == "breakdown")
    builds = [e.t for e in events if e.kind == "build"]
    assert builds
    # no drop is announced in the middle of a build (it must be the bass arriving)
    for e in events:
        if e.kind == "drop":
            sec = src.section_at(e.t)
            assert sec in ("drop", "build"), (e.t, sec)


def test_silence_is_silent_and_recovers():
    from audio.sources import SilentSource
    silent = SilentSource()
    fx = FeatureExtractor(silent.sr, silent.hop)
    an = MusicAnalyzer(fx.fps, 80)
    rng = np.random.default_rng(1)
    ev = []
    for n in range(int(20 * silent.sr / silent.hop)):
        f = fx.process(rng.standard_normal(silent.hop).astype(np.float32) * 0.0004, n * silent.hop / silent.sr)
        ev += an.process(f)
    kinds = {e.kind for e in ev}
    assert "beat" not in kinds and "onset_mid" not in kinds and "onset_treble" not in kinds and "drop" not in kinds
    assert an.silent
    # music starts again
    src = SyntheticSource(realtime=False)
    t0 = 20.0
    ev = []
    for n in range(int(10 * src.sr / src.hop)):
        i = (n * src.hop + int(8 * src.sr)) % len(src._loop)
        f = fx.process(src._loop[i:i + src.hop][: src.hop], t0 + n * src.hop / src.sr)
        ev += an.process(f)
    assert any(e.kind == "resume" for e in ev) and not an.silent


def test_steady_tone_does_not_chatter():
    fx = FeatureExtractor(22050, 512)
    an = MusicAnalyzer(fx.fps, 90)
    ev = []
    for n, b in enumerate(tone(110, 0.3, n=1300)):
        ev += an.process(fx.process(b, n * 512 / 22050))
    onsets = [e for e in ev if e.kind in ("beat", "onset_mid", "onset_treble")]
    assert len(onsets) <= 3, len(onsets)


def test_higher_sensitivity_never_finds_fewer_onsets():
    src = SyntheticSource(realtime=False)
    n = {}
    for s in (10, 50, 95):
        _, ev, _ = run_source(src, 60, sensitivity=s)
        n[s] = sum(e.kind in ("onset_mid", "onset_treble") for e in ev)
    assert n[10] <= n[50] <= n[95], n
    assert n[95] > n[10]


def test_calibration_quality_flags():
    src = SyntheticSource(realtime=False)
    _, _, frames = run_source(src, 12)
    ok = build_calibration("demo", src.sr, frames, 10)
    assert ok.quality == "ok" and ok.rms_peak > ok.rms_floor
    # nothing but room noise
    fx = FeatureExtractor(22050, 512)
    rng = np.random.default_rng(2)
    quiet_frames = [fx.process(rng.standard_normal(512).astype(np.float32) * 0.0003, i * 0.023) for i in range(300)]
    assert build_calibration("x", 22050, quiet_frames, 6).quality in ("no_signal", "too_quiet")
    # hard-clipped input
    clipped = [fx.process(np.sign(rng.standard_normal(512)).astype(np.float32), i * 0.023) for i in range(300)]
    assert build_calibration("x", 22050, clipped, 6).quality == "too_loud"
    from audio.analyzer import Calibration
    assert Calibration.from_json(ok.to_json()).rms_peak == ok.rms_peak


def test_calibrated_analyzer_still_finds_the_beat():
    src = SyntheticSource(realtime=False)
    _, _, frames = run_source(src, 12)
    cal = build_calibration("demo", src.sr, frames, 10)
    an, ev, _ = run_source(src, 48, cal=cal)          # ends inside a drop, where the pulse is clear
    assert sum(e.kind == "beat" for e in ev) > 40
    assert an.bpm and abs(an.bpm - 124) <= 3, an.bpm
