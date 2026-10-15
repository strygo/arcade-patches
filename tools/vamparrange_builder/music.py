"""Reconstruct the shared Vampire Arrange CPK from verified Collection slices."""
import argparse
import hashlib
import json
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
    p.add_argument('--iso', required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    import collection
    value = json.loads(Path(__file__).with_name('music.json').read_text())
    a.out.parent.mkdir(parents=True, exist_ok=True)
    build(collection.Disc(a.iso), value, a.out)
    print('Built shared Vampire Arrange music pack; all sources and output verified.')


if __name__ == '__main__':
    main()
