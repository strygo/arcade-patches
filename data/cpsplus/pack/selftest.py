"""`build_pack.py selftest` — writer/reader round-trip on synthetic data.

Covers: binary layout round-trip (header/protocol/triggers/index), CRC
corruption detection, ffmpeg ADX encode->decode fidelity, the pure-Python
reference decoder vs ffmpeg (bit-exact), audition loop assembly math, and
the structural verifier.
"""
from __future__ import annotations

import array
import math
import shutil
from pathlib import Path

from . import adxcodec, protocols, xfade
from .audition import render, verify
from .format import (PackReader, PackWriter, TrackMeta, TriggerRow,
                     CODEC_ADX, CODEC_PCM, VERB_PLAY, VERB_STOP)

WORK = Path(__file__).resolve().parent.parent / "work" / "tmp"


def _sine_pcm(n: int, rate: int, freq: float, ch: int = 2) -> bytes:
    a = array.array("h")
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / rate))
        for c in range(ch):
            a.append(v if c == 0 else -v)
    return a.tobytes()


def run() -> bool:
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool):
        checks.append((name, ok))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")

    WORK.mkdir(parents=True, exist_ok=True)
    rate, ch = 32000, 2
    intro_n, body_n = 12800, 25600           # 0.4 s + 0.8 s, 32-aligned,
    total_n = intro_n + body_n               # 440 Hz is loop-continuous
    pcm = _sine_pcm(total_n, rate, 440.0, ch)

    # track 0: looped ADX
    stream = adxcodec.encode(pcm, ch, rate)
    c1, c2 = adxcodec.calc_coeffs(500, rate)
    m0 = TrackMeta(sample_rate=rate, channels=ch, codec=CODEC_ADX, gain=0x70,
                   loop_start_sample=intro_n,
                   loop_start_byte=adxcodec.samples_to_stream_byte(intro_n, ch),
                   loop_end_sample=total_n,
                   loop_end_byte=adxcodec.samples_to_stream_byte(total_n, ch),
                   coef1=c1, coef2=c2, name="sine_loop", source="synthetic")
    # track 1: one-shot PCM
    pcm1 = _sine_pcm(8000, rate, 880.0, ch)
    m1 = TrackMeta(sample_rate=rate, channels=ch, codec=CODEC_PCM, gain=0x7f,
                   name="sine_oneshot", source="synthetic")

    proto = protocols.get_protocol("sfa1")
    w = PackWriter(proto, title="selftest pack")
    t0 = w.add_track(stream[:m0.loop_end_byte], m0)
    t1 = w.add_track(pcm1, m1)
    w.set_trigger(0x10, TriggerRow(verb=VERB_PLAY, track=t0, gain=0x60,
                                   suppress=1))
    w.set_trigger(0x11, TriggerRow(verb=VERB_PLAY, track=t1, gain=0x7f,
                                   suppress=1))
    w.set_trigger(0xff, TriggerRow(verb=VERB_STOP, suppress=1))
    pack_path = WORK / "selftest.cpk"
    w.write(pack_path)

    rd = PackReader(pack_path)
    h = rd.header
    check("header identity", (h.game_id, h.title) == ("sfa1", "selftest pack"))
    check("protocol round-trip",
          rd.header.proto.control_verbs == proto.control_verbs
          and rd.header.proto.fade_law == proto.fade_law
          and rd.header.proto.fade_const1 == proto.fade_const1
          and rd.header.proto.latch_page == 0x618000)
    check("trigger round-trip",
          rd.triggers[0x10] == TriggerRow(VERB_PLAY, t0, 0x60, 1)
          and rd.triggers[0x11] == TriggerRow(VERB_PLAY, t1, 0x7f, 1)
          and rd.triggers[0xff] == TriggerRow(VERB_STOP, 0, 0x7f, 1)
          and all(r.verb == 0 for i, r in enumerate(rd.triggers)
                  if i not in (0x10, 0x11, 0xff)))
    got0, got1 = rd.tracks[0], rd.tracks[1]
    check("track meta round-trip",
          (got0.sample_rate, got0.channels, got0.codec, got0.gain,
           got0.loop_start_sample, got0.loop_end_byte, got0.coef1, got0.coef2)
          == (rate, ch, CODEC_ADX, 0x70, intro_n, m0.loop_end_byte, c1, c2)
          and (got1.codec, got1.loops) == (CODEC_PCM, False))
    check("track data round-trip",
          rd.read_track(0) == stream[:m0.loop_end_byte]
          and rd.read_track(1) == pcm1)
    ok_t, ok_d = rd.crc_check()
    check("CRCs valid", ok_t and ok_d)

    # ffmpeg decode vs pure-Python reference decoder (bit-exact)
    sub = rd.read_track(0)[:36 * 200]     # 200 stereo frame pairs
    ff = adxcodec.decode(sub, ch, rate)
    py = adxcodec.py_decode(sub, ch, c1, c2)
    ffa = array.array("h")
    ffa.frombytes(ff)
    match = all(ffa[i * ch + c] == py[c][i]
                for i in range(len(py[0])) for c in range(ch))
    check("py_decode == ffmpeg decode (bit-exact)", match)

    # codec fidelity: decoded sine correlates with the source
    dec = adxcodec.decode(rd.read_track(0), ch, rate)
    da = array.array("h")
    da.frombytes(dec)
    sa = array.array("h")
    sa.frombytes(pcm)
    n = min(len(da), len(sa))
    sab = saa = sbb = 0
    for i in range(0, n, 7):              # stride: keep it quick
        x, y = sa[i], da[i]
        sab += x * y
        saa += x * x
        sbb += y * y
    ncc = sab / math.sqrt(saa * sbb)
    check(f"ADX encode/decode fidelity (NCC={ncc:.4f})", ncc > 0.95)

    # audition loop assembly: intro + 2 loop passes
    out, _ = render(rd, 0, loops=2)
    want = (total_n + 2 * body_n) * 2 * ch
    check("audition loop assembly length", len(out) == want)
    seam = render(rd, 0, loops=1)[0]
    fb = 2 * ch
    a = array.array("h")
    a.frombytes(seam[(total_n - 2) * fb:(total_n + 2) * fb])
    steps = [abs(a[(i + 1) * ch] - a[i * ch]) for i in range(3)]
    # a continuous 440 Hz sine at 32 kHz moves at most
    # 2*pi*440/32000*12000 ~= 1036 units/sample; a seam glitch would jump
    # by up to 2*amplitude = 24000.
    check(f"loop seam continuous on synthetic sine (steps={steps})",
          max(steps) < 1300)
    check("verify() passes", verify(pack_path, quick=True))
    rd.close()

    # ---- format v1 loop crossfade (byte-exact tail retention) --------------
    xn = 1600                                    # 50 frames, 32-aligned
    xls, xle = 6400, 32000                       # frame-aligned, le-ls >> xn
    xpcm = _sine_pcm(40000, rate, 437.0, ch)     # 437 Hz: loop NOT continuous
    xstream = adxcodec.encode(xpcm, ch, rate)
    xls_b = adxcodec.samples_to_stream_byte(xls, ch)
    xle_b = adxcodec.samples_to_stream_byte(xle, ch)
    xtail_b = adxcodec.samples_to_stream_byte(xn, ch)
    mx = TrackMeta(sample_rate=rate, channels=ch, codec=CODEC_ADX, gain=0x7f,
                   xfade_enable=1, coef1=c1, coef2=c2, name="xfade_loop",
                   source="synthetic", loop_start_sample=xls,
                   loop_start_byte=xls_b, loop_end_sample=xle,
                   loop_end_byte=xle_b)
    wx = PackWriter(proto, title="xfade selftest", xfade_samples=xn)
    tx = wx.add_track(xstream[:xle_b + xtail_b], mx)
    wx.set_trigger(0x20, TriggerRow(verb=VERB_PLAY, track=tx, suppress=1))
    xpath = WORK / "selftest_xfade.cpk"
    wx.write(xpath)
    rdx = PackReader(xpath)
    mgot = rdx.tracks[0]
    check("xfade header/track round-trip (v1)",
          rdx.header.format_version == 1 and rdx.header.xfade_samples == xn
          and mgot.xfade_enable == 1
          and mgot.data_length == xle_b + xtail_b)
    # audition.render must equal the shared model (pack/xfade.py == RTL)
    dec = adxcodec.decode(rdx.read_track(0), ch, rate,
                          total_samples=(xle_b + xtail_b) // (18 * ch) * 32)
    da2 = array.array("h")
    da2.frombytes(dec)
    pairs = [(da2[i * ch], da2[i * ch + 1]) for i in range(len(da2) // ch)]
    model = xfade.render(pairs, xls, xle, xn, loops=1)
    aud, _ = render(rdx, 0, loops=1)
    aa = array.array("h")
    aa.frombytes(aud)
    aud_pairs = [(aa[i * ch], aa[i * ch + 1]) for i in range(len(aa) // ch)]
    check("audition crossfade == shared xfade model (bit-exact)",
          aud_pairs == model)
    # the blended seam is continuous where a raw hard cut would jump
    hardcut_jump = max(abs(pairs[xle][c] - pairs[xls][c]) for c in range(ch))
    seam_step = max(abs(model[xle][c] - model[xle - 1][c]) for c in range(ch))
    check(f"crossfade seam smooth (hardcut jump {hardcut_jump} -> "
          f"seam step {seam_step})", seam_step < hardcut_jump)
    rdx.close()
    # the tail-length guard rejects a truncated (no-tail) crossfade stream
    bad = False
    try:
        PackWriter(proto, xfade_samples=xn).add_track(xstream[:xle_b], mx)
    except ValueError:
        bad = True
    check("add_track rejects a crossfade stream with no tail", bad)

    # corruption detection
    blob = bytearray(pack_path.read_bytes())
    blob[h.data_offset + 100] ^= 0xff
    bad = WORK / "selftest_bad.cpk"
    bad.write_bytes(blob)
    shutil.copy(str(pack_path) + ".json", str(bad) + ".json")
    rdb = PackReader(bad)
    okt, okd = rdb.crc_check()
    check("data corruption detected", okt and not okd)
    rdb.close()

    failed = [n for n, ok in checks if not ok]
    print(f"selftest: {len(checks) - len(failed)}/{len(checks)} passed"
          + (f" — FAILED: {failed}" if failed else ""))
    return not failed
