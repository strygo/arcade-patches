"""Build the album-sourced packs: Double Impact (Final Fight, Magic Sword) and
SSF2T HD Remix.

Each pack's recipe, manifests/<pack>_audio.json, pins the album files by
SHA-256 and records the exact trim, loop and crossfade coordinates that were
reviewed, so the same album files always give the same pack.  The album
itself is the user's copy:

  Double Impact  Capcom's free 2010 release, "Final Fight-Double Impact
                 Remixed Soundtrack.zip" (http://www.finalfightgame.com/remix),
                 unpacked; both packs come from the same download.
  HD Remix       OC ReMix's official soundtrack OCRA-0012, the FLAC set
                 (https://ocremix.org/album/12).

Decoding is integer-only end to end (album_audio.py, resample.py), so a
pack rebuilt on any CPU matches the pinned hash; the build record notes the
ffmpeg that did the decoding and ADX encoding.

Pass the folder holding the album with --source-root.  Files are located by
the album's own layout first and by name otherwise, so a renamed folder still
works.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from decimal import ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
import zipfile
import numpy as np

from . import adxcodec, protocols, resample, xfade
from .album_audio import RATE, decode, locate, sha256
from .build_common import MANIFESTS, PACKS_DIR, REPO_ROOT
from .build_ffight_arrange import load_triggers
from .format import PackWriter, PackReader, TrackMeta, TriggerRow, VERB_PLAY, VERB_NONE

PACKS={
    'ffight_di':('Final Fight (Double Impact Remix)','final_fight_double_impact_remixed_soundtrack_2010','ffight'),
    'msword_di':('Magic Sword (Double Impact Remix)','final_fight_double_impact_remixed_soundtrack_2010','msword'),
    'ssf2t_hdremix':('Super Street Fighter II Turbo (HD Remix)','ssf2t_hd_remix_ocremix_ost','ssf2t'),
}
FF_TRACKS={'tr02':0x40,'tr03':0x41,'tr04':0x42,'tr05':0x43,'tr06':0x44,'tr07':0x45,
           'tr08':0x46,'tr09':0x47,'tr10':0x48,'tr11':0x49,'tr12':0x4c,'tr13':0x50,
           'tr14':0x51,'tr15':0x58,'tr16':0x57,'tr17':0x55,'tr18':0x53,'tr25':0x52,'tr26':0x54}

def validate(row, frames):
    start,n,ls,le=(row[k] for k in ['trim_start','keep_samples','loop_start','loop_end'])
    if not 0<=start<frames or not 0<n<=frames-start: raise ValueError('source trim out of bounds')
    if le and not (0<=ls<le<=n and ls%32==0 and le%32==0): raise ValueError('invalid ADX loop')
    if row['loop_count'] and not le: raise ValueError('finite pass without loop')
    if row['xfade_samples'] and le+row['xfade_samples']>n: raise ValueError('missing real crossfade tail')
    if row.get('fade_in_samples',0)>n: raise ValueError('fade-in longer than the track')

def bake_crossfade(pcm, row):
    """Store the authored blend as PCM so fixed-length FPGA LUTs are unused.

    An infinite loop resumes after the blended head. Moving both endpoints
    by N preserves the intro, every sample of the blend, and the loop period.
    Finite loops must retain their unblended final outro, so expand those
    wraps into a one-shot instead of changing the final pass.
    """
    row = dict(row)
    n = row['xfade_samples']
    if not n:
        return pcm, row
    ls, le = row['loop_start'], row['loop_end']
    if n % 32 or le - ls <= n or len(pcm) < le + n:
        raise ValueError('crossfade needs aligned, nonoverlapping head and tail')
    lut = np.array(xfade.make_lut(n), dtype=np.int64)[:, None]
    blend = np.clip((pcm[le:le+n].astype(np.int64)*lut +
                     pcm[ls:ls+n].astype(np.int64)*lut[::-1] + 16384) >> 15,
                    -32768, 32767).astype('<i2')
    intro = np.concatenate((pcm[:le], blend))
    if row['loop_count']:
        body = np.concatenate((pcm[ls+n:le], blend))
        pcm = np.concatenate([intro] + [body]*(row['loop_count']-1) + [pcm[ls+n:]])
        row.update(loop_start=0, loop_end=0, loop_count=0)
    else:
        pcm = intro
        row.update(loop_start=ls+n, loop_end=le+n)
    row.update(keep_samples=len(pcm), xfade_samples=0)
    return pcm, row


FADE_BITS=40

@lru_cache(maxsize=None)
def fade_in_gains(n):
    """Raised-cosine gains sin(pi*k/(2n))**2, k < n, as Q40 integers.

    Worked in decimal arithmetic and rounded half-even, so no libm sits
    between the recipe and the pack bytes."""
    with localcontext() as ctx:
        ctx.prec=30
        return np.array([int((resample.sin_pi(Fraction(k,2*n),30)**2*(1<<FADE_BITS))
                             .to_integral_value(rounding=ROUND_HALF_EVEN)) for k in range(n)],dtype=np.int64)

def bake_fade_in(pcm, n):
    """Raised-cosine fade-in over the first n samples, baked into the PCM
    (gain applied in int64, rounded half up; a gain <= 1 cannot clip)."""
    if not n: return pcm
    head=(pcm[:n].astype(np.int64)*fade_in_gains(n)[:,None]+(1<<(FADE_BITS-1)))>>FADE_BITS
    return np.concatenate((head.astype('<i2'),pcm[n:]))

def build(key, source_root, outdir):
    title,album,game=PACKS[key]
    recipe=json.loads((MANIFESTS/f'{key}_audio.json').read_text())
    if source_root is None:
        source_root=REPO_ROOT/'roms/soundtracks'/album
        if not source_root.is_dir():
            raise SystemExit(f'{key}: pass --source-root, the folder holding the album files')
    proto=protocols.cps1_protocol() if game=='ffight' else protocols.PROTOCOLS[game]
    outdir.mkdir(parents=True,exist_ok=True)
    name=key
    w=PackWriter(proto,title=title,
                 trigger_rows=(256 if game in ('msword','ffight') else 512),
                 default_rate=RATE,xfade_samples=0)
    tracks={}; used={}; audit=[]
    for row in recipe['tracks']:
        row=dict(row); cmd=row['cmd']; src=locate(source_root,row['source'])
        if sha256(src)!=row['source_sha256']:
            raise ValueError(f'{src}: not the album file this pack was made from (hash mismatch)')
        pcm=decode(src)
        validate(row,len(pcm))
        # Duplicate arcade UI keys may share one encoded track.
        sig=(row['source_sha256'],row['trim_start'],row['keep_samples'],row['loop_start'],
             row['loop_end'],row['loop_count'],row['xfade_samples'],row.get('fade_in_samples',0))
        if sig in used: tracks[cmd]=used[sig]; continue
        pcm=pcm[row['trim_start']:row['trim_start']+row['keep_samples']]
        pcm=bake_fade_in(pcm,row.get('fade_in_samples',0))
        pcm,row=bake_crossfade(pcm,row)
        data=adxcodec.encode(pcm.astype('<i2').tobytes(),2,RATE)
        c1,c2=adxcodec.calc_coeffs(adxcodec.DEFAULT_CUTOFF,RATE)
        meta=TrackMeta(sample_rate=RATE,channels=2,gain=127,coef1=c1,coef2=c2,
                       loop_start_sample=row['loop_start'],loop_end_sample=row['loop_end'],
                       loop_start_byte=adxcodec.samples_to_stream_byte(row['loop_start'],2),
                       loop_end_byte=adxcodec.samples_to_stream_byte(row['loop_end'],2),
                       loop_count=row['loop_count'],xfade_enable=bool(row['xfade_samples']),
                       name=row['title'],source=row['source'])
        ti=w.add_track(data,meta); tracks[cmd]=ti; used[sig]=ti
        audit.append(dict(cmd=f'0x{cmd:02x}',track=ti,source_sha256=row['source_sha256'],
                          pcm_sha256=hashlib.sha256(pcm.astype('<i2').tobytes()).hexdigest()))
        print(name,hex(cmd),row['title'],f'{len(pcm)/RATE:.3f}s',flush=True)
    gain=recipe['trigger_gain']
    if game=='ffight':
        # Both opening cues reference one track: CPS1 SAME_SONG ignores the
        # second PLAY, so each region starts on its own earliest cue.
        for r in load_triggers(MANIFESTS/'ffight_di_trigger_map.tsv'):
            w.set_trigger(r.cmd,TriggerRow(verb=r.verb,track=tracks[int(r.track,16)] if r.track else 0,
                                          gain=gain if r.verb==VERB_PLAY else 127,suppress=r.suppress))
    else:
        for cmd,ti in tracks.items(): w.set_trigger(cmd,TriggerRow(verb=VERB_PLAY,track=ti,gain=gain,suppress=1))
    pack=outdir/f'{name}.cpk'; w.write(pack)
    rd=PackReader(pack)
    try:
        for cmd in recipe['fail_open']:
            r=rd.triggers[cmd]
            if r.verb!=VERB_NONE or r.suppress: raise ValueError(f'fail-open command gated: {cmd:#x}')
    finally: rd.close()
    report=dict(pack=pack.name,sha256=sha256(pack),bytes=pack.stat().st_size,
                review_status=recipe['review_status'],ffmpeg=adxcodec.ffmpeg_version(),
                resample_table_sha256=resample.TABLE_SHA256[(44100,RATE)],tracks=audit)
    (outdir/f'{name}.build.json').write_text(json.dumps(report,indent=2)+'\n')
    # Fixed archive metadata: zip bytes as well as CPK bytes reproduce.
    with zipfile.ZipFile(outdir/f'{name}.zip','w',compression=zipfile.ZIP_STORED) as z:
        zi=zipfile.ZipInfo(pack.name,date_time=(2026,9,4,0,0,0)); zi.external_attr=0o100644<<16
        z.writestr(zi,pack.read_bytes())
    return report

def self_test():
    row=dict(trim_start=0,keep_samples=96000,loop_start=0,loop_end=48000,loop_count=0,xfade_samples=4800)
    validate(row,96000)
    for changes in [dict(loop_end=48001),dict(keep_samples=100000),dict(trim_start=-1),dict(xfade_samples=96000)]:
        try: validate(row|changes,96000)
        except ValueError: pass
        else: raise AssertionError(changes)
    assert len(FF_TRACKS)==19 and set(FF_TRACKS.values())==set(range(0x40,0x4a))|{0x4c,0x50,0x51,0x52,0x53,0x54,0x55,0x57,0x58}
    rows={r.cmd:r for r in load_triggers(MANIFESTS/'ffight_di_trigger_map.tsv')}
    # the opening plays on the SONG cue only; ring and click are the board's own
    assert rows[0x52].verb==VERB_PLAY and int(rows[0x52].track,16)==0x52 and rows[0x52].suppress==1
    assert rows[0x35].verb==VERB_NONE and rows[0x35].suppress==0
    assert rows[0x36].verb==VERB_NONE and rows[0x36].suppress==0
    faded=bake_fade_in(np.full((100,2),1000,dtype='<i2'),50)
    assert faded[0,0]==0 and faded[49,0]<1000 and faded[50,0]==1000
    # Compare stored hard-loop playback against the independent reference
    # for infinite and finite crossfades, including nonzero loop starts.
    pcm=np.random.default_rng(19).integers(-32000,32001,(448,2),dtype=np.int16)
    for count in (0,1,3):
        authored=dict(loop_start=64,loop_end=320,loop_count=count,xfade_samples=32)
        baked,meta=bake_crossfade(pcm,authored)
        if count:
            expected=xfade.render(pcm.tolist(),64,320,32,count-1)+pcm[96:].tolist()
            actual=baked
            assert meta['loop_end']==0 and meta['loop_count']==0
        else:
            expected=xfade.render(pcm.tolist(),64,320,32,2)
            actual=np.concatenate([baked,baked[meta['loop_start']:meta['loop_end']],
                                   baked[meta['loop_start']:meta['loop_end']]])
            assert meta['loop_end']-meta['loop_start']==256
        assert np.array_equal(actual,np.asarray(expected)),count
        assert meta['xfade_samples']==0
    for changes in [dict(xfade_samples=33), dict(loop_end=448), dict(loop_start=288)]:
        try:
            bake_crossfade(pcm,dict(loop_start=64,loop_end=320,loop_count=0,xfade_samples=32)|changes)
        except ValueError:
            pass
        else:
            raise AssertionError(changes)
    # The resampler's taps are exact, so table() checks the one digest they
    # can have.  A 16-bit tone and the same tone as 24-bit give the same s16.
    resample.table(44100,RATE)
    tone=np.round(np.sin(np.arange(4410)*2*np.pi*1000/44100)*20000).astype(np.int32)[:,None]
    out16=resample.resample(np.hstack([tone,-tone]),44100,RATE)
    out24=resample.resample(np.hstack([tone,-tone])<<8,44100,RATE,8)
    assert len(out16)==4800 and np.array_equal(out16,out24)
    assert 19990<=out16[100:-100,0].max()<=20010
    print('remix builder self-test passed')

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pack',choices=[*PACKS,'all'],default='all')
    ap.add_argument('--source-root',type=Path,help='folder holding the album files')
    ap.add_argument('--out-dir',type=Path,default=PACKS_DIR)
    ap.add_argument('--self-test',action='store_true')
    a=ap.parse_args(argv)
    if a.self_test: self_test(); return 0
    for key in PACKS if a.pack=='all' else [a.pack]:
        build(key,a.source_root,a.out_dir)
    return 0
