"""PS2 VAG voices -> CPS-2 QSound 8-bit samples (decode, resample, quantize).

Each conversion the kit performs is listed in its recipes (disc entry, VAG,
source and target rate); the output is checked against the known build.
"""
import math

ADPCM = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))


def decode_vag(data):
    out, h1, h2 = [], 0, 0
    for o in range(0, len(data) - 15, 16):
        p, s = data[o] >> 4, data[o] & 15
        c1, c2 = ADPCM[p if p < 5 else 0]
        for b in data[o + 2:o + 16]:
            for n in (b & 15, b >> 4):
                n = n - 16 if n & 8 else n
                v = ((n << 12) >> s) + ((h1 * c1 + h2 * c2 + 32) >> 6)
                v = -32768 if v < -32768 else 32767 if v > 32767 else v
                out.append(v)
                h2, h1 = h1, v
    return out


def resample(x, src, dst, taps=8):
    """Windowed-sinc (Hann, +-taps) resampling; the cutoff follows the lower rate."""
    if not x:
        return []
    ratio = src / dst
    fc = min(1.0, dst / src)
    n_out = int(len(x) / ratio)
    out = []
    for n in range(n_out):
        t = n * ratio
        i0 = int(math.floor(t))
        acc = 0.0
        wsum = 0.0
        for i in range(i0 - taps + 1, i0 + taps + 1):
            if i < 0 or i >= len(x):
                continue
            d = (t - i) * fc
            sinc = 1.0 if d == 0 else math.sin(math.pi * d) / (math.pi * d)
            win = 0.5 + 0.5 * math.cos(math.pi * (t - i) / (taps + 1))
            k = fc * sinc * win
            acc += x[i] * k
            wsum += k
        out.append(acc / wsum if wsum else 0.0)
    return out


def to_pcm8(y):
    """s16 -> s8 by arithmetic >> 8 of the rounded value."""
    b = bytearray()
    for v in y:
        s = int(round(v))
        s = -32768 if s < -32768 else 32767 if s > 32767 else s
        b.append((s >> 8) & 0xFF)
    return bytes(b)


def convert(vag, rate_in, rate_out):
    """One VAG as QSound 8-bit sample bytes at rate_out (no resample when equal)."""
    pcm = decode_vag(vag)
    if abs(rate_out - rate_in) > 1e-6:
        pcm = resample(pcm, rate_in, rate_out)
    return to_pcm8(pcm)
