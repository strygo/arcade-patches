"""Reconstruct the shared Vampire Arrange CPK from verified Collection slices."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import cpk_format


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def build(disc, value, path):
    protocol = dict(value['protocol'])
    protocol['control_verbs'] = {int(k): v for k, v in protocol['control_verbs'].items()}
    writer = cpk_format.PackWriter(cpk_format.Protocol(**protocol), title=value['title'])
    for command in value['native_music_handoffs']:
        writer.set_trigger(command, cpk_format.TriggerRow(verb=cpk_format.VERB_STOP, suppress=0))
    for index, row in enumerate(value['tracks']):
        raw = disc._read(row['offset'], row['bytes'])
        require(sha(raw) == row['source_sha256'], 'Collection music source differs')
        fields = dict(row['metadata'])
        length = fields.pop('data_length')
        fields.pop('data_offset')
        start = row['adx_data_offset']
        stream = raw[start:start + length]
        require(sha(stream) == row['stream_sha256'], 'Collection music frames differ')
        writer.add_track(stream, cpk_format.TrackMeta(**fields))
        writer.set_trigger(row['command'], cpk_format.TriggerRow(
            verb=cpk_format.VERB_PLAY, track=index, gain=20, suppress=1))
    writer.write(path, sidecar=False)
    require((path.stat().st_size, sha(path.read_bytes())) == (value['bytes'], value['sha256']),
            'complete reconstructed music pack differs')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iso','--disc',dest='iso',required=True,help='Collection DVD ISO or CHD')
    p.add_argument('--chdman')
    p.add_argument('--debug',action='store_true')
    p.add_argument('--out', type=Path, required=True)
    from tool_controls import add_arguments,configure
    add_arguments(p)
    a = p.parse_args()
    configure(sys.argv[1:])
    import collection
    value = json.loads(Path(__file__).with_name('music.json').read_text())
    from disc_sources import dvd_iso
    from rom_sources import check_output,require_path
    a.iso=require_path(a.iso,'--disc',file=True)
    check_output(a.out,[a.iso],directory=False)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with dvd_iso(a.iso,chdman=a.chdman) as iso:
        build(collection.Disc(iso), value, a.out)
    print('Built shared Vampire Arrange music pack; all sources and output verified.')


if __name__ == '__main__':
    import sys
    try:main()
    except (OSError,ValueError) as exc:
        if '--debug' in sys.argv:raise
        raise SystemExit('ERROR: '+str(exc))
