"""Build the Strider (PSX Soundtrack) pack from the sound-test recording.

The recording is made by tools/capture_strider_psx.py (MAME driving the
PlayStation port's own sound test); the recipe pins its hash and the exact
trim and loop coordinates of the ten reviewed performances.

The approved PCM already has linear blends baked before each loop boundary.
Preserve those exact edits; enabling the core's equal-power crossfade as well
would change the reviewed join. No PCE/XA audio is used by this builder.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import wave
import zipfile

import numpy as np

from . import adxcodec, protocols
from .build_common import MANIFESTS, PACKS_DIR, PKG_ROOT
from .format import PackWriter, TrackMeta, TriggerRow, VERB_PLAY, VERB_STOP

RECIPE = MANIFESTS / "strider_psx_audio.json"
CAPTURE = PKG_ROOT / "work" / "strider_psx" / "audio.wav"   # capture_strider_psx.py's default output
RATE = 48000


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def validate(row):
    a, b, n = row['loop_start'], row['loop_end'], row['crossfade_frames']
    if not (0 < n <= a < b and n <= b-a and all(v % 32 == 0 for v in (a,b,n))):
        raise ValueError('invalid aligned loop/blend coordinates')
    if row['capture_trim_sample'] < 0:
        raise ValueError('negative source position')


def pcm_for(row, capture):
    validate(row)
    a, b, n = row['loop_start'], row['loop_end'], row['crossfade_frames']
    with wave.open(str(capture)) as f:
        if (f.getframerate(), f.getnchannels(), f.getsampwidth()) != (RATE,2,2):
            raise ValueError('capture must be 48 kHz stereo s16 PCM')
        f.setpos(row['capture_trim_sample'])
        raw = f.readframes(b)
    if len(raw) != b*4:
        raise ValueError('capture ends before loop boundary')
    x = np.frombuffer(raw, '<i2').reshape(-1,2).astype(np.float64)/32768
    y = x.copy()
    weights = np.linspace(0.,1.,n)[:,None]
    y[b-n:b] = x[b-n:b]*(1-weights) + x[a-n:a]*weights
    pcm = np.rint(y*32768).clip(-32768,32767).astype('<i2').tobytes()
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as f:
        f.setparams((2,2,RATE,0,'NONE','not compressed')); f.writeframes(pcm)
    if hashlib.sha256(buf.getvalue()).hexdigest() != row['reviewed_wav_sha256']:
        raise ValueError(f"{row['id']}: reconstructed PCM differs from reviewed recording")
    return pcm


def stored_zip(path, files):
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as z:
        for name, data in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,9,5,0,0,0))
            info.external_attr=0o100644<<16
            z.writestr(info,data)


def build(recipe_path=RECIPE, capture=None, outdir=PACKS_DIR):
    recipe=json.loads(Path(recipe_path).read_text())
    capture=Path(capture or CAPTURE)
    if not capture.exists():
        raise SystemExit(f'{capture}: no recording. Make it with tools/capture_strider_psx.py, or pass --capture')
    if digest(capture)!=recipe['source']['capture_sha256']:
        raise ValueError(f'{capture}: not the reviewed recording (hash mismatch)')
    outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    w=PackWriter(protocols.PROTOCOLS['strider'],title='Strider (PSX Soundtrack)',
                 trigger_rows=256,default_rate=RATE,xfade_samples=0)
    tracks={};audit=[]
    for row in recipe['tracks']:
        pcm=pcm_for(row,capture)
        stream=adxcodec.encode(pcm,2,RATE)
        c1,c2=adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF,RATE)
        meta=TrackMeta(sample_rate=RATE,channels=2,gain=127,coef1=c1,coef2=c2,
                       loop_start_sample=row['loop_start'],loop_end_sample=row['loop_end'],
                       loop_start_byte=adxcodec.samples_to_stream_byte(row['loop_start'],2),
                       loop_end_byte=adxcodec.samples_to_stream_byte(row['loop_end'],2),
                       xfade_enable=False,name=row['title'],source=recipe['source']['disc'])
        tracks[row['id']]=w.add_track(stream,meta)
        audit.append(dict(id=row['id'],pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                          adx_sha256=hashlib.sha256(stream).hexdigest()))
        print(row['id'],row['title'],flush=True)
    for row in recipe['triggers']:
        cmd=int(row['command'],16)
        if row['track']:
            w.set_trigger(cmd,TriggerRow(verb=VERB_PLAY,track=tracks[row['track']],
                                         gain=recipe['trigger_gain'],suppress=1))
        elif row['stop_arranged']:
            # Native fallback must stop the replacement, without suppressing
            # the original cue. Credit is an effect and does not stop music.
            w.set_trigger(cmd,TriggerRow(verb=VERB_STOP,suppress=0))
    pack=outdir/'strider_psx.cpk';w.write(pack)
    stored_zip(outdir/'strider_psx.zip',{pack.name:pack.read_bytes()})
    report=dict(pack=pack.name,sha256=digest(pack),bytes=pack.stat().st_size,
                review_status=recipe['review_status'],recipe_sha256=digest(recipe_path),
                tracks=audit)
    (outdir/'strider_psx.build.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def self_test():
    row=dict(loop_start=64,loop_end=192,crossfade_frames=32,capture_trim_sample=0)
    validate(row)
    for delta in [dict(loop_end=193),dict(crossfade_frames=96),dict(loop_start=192),
                  dict(capture_trim_sample=-1)]:
        try: validate(row|delta)
        except ValueError: pass
        else: raise AssertionError(delta)
    print('Strider PSX source-coordinate checks passed')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--recipe',type=Path,default=RECIPE)
    p.add_argument('--capture',type=Path,help='the sound-test recording (default: work/strider_psx/audio.wav)')
    p.add_argument('--out-dir',type=Path,default=PACKS_DIR)
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args(argv)
    if a.self_test:self_test();return 0
    print(json.dumps(build(a.recipe,a.capture,a.out_dir),indent=2));return 0
