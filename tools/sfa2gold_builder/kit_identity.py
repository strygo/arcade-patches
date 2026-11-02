"""Gold-family identity and dependency listing, with no media/runtime imports."""
import json
from pathlib import Path, PurePosixPath

PLATFORMS = ('mame', 'hbmame', 'fbneo', 'mister', 'darksoft')


def load_config(root):
    kit = json.loads((Path(root) / 'kit.json').read_text())
    if kit.get('edition') not in ('sfa2-gold', 'sfa2-ex'):
        raise ValueError('Unknown Gold-family edition')
    if not kit['sets'] or set(kit['sets']) - {'us', 'eu', 'jp', 'asia'}:
        raise ValueError('Unknown Gold-family region')
    if set(kit['sets']) != set(kit['mra_dir']):
        raise ValueError('Region and MRA folder declarations differ')
    for region, names in kit['sets'].items():
        if names['4mb'] != ('sfz2alj' if region == 'jp' else 'sfz2al'):
            raise ValueError('Unknown Gold-family arcade source')
    for name in [*kit['mra_root'], *kit['mra_dir'].values(),
                 *(s[k] for s in kit['sets'].values() for k in ('4mb', '8mb', 'mra'))]:
        if name and (PurePosixPath(name).name != name or name in ('.', '..') or '\\' in name):
            raise ValueError('Kit identity contains an unsafe path')
    return kit


def describe(root):
    root = Path(root)
    kit = load_config(root)
    catalog = json.loads((root / 'rom_inputs.json').read_text())['sets']
    return {'schema': 1, 'kind': 'gold-family-description', 'id': kit['edition'],
            'regions': {region: {'sets': names, 'disc_region': 'us' if region == 'asia' else region,
                                'arcade_inputs': [s for s in catalog[names['4mb']].values()
                                                  if s['role'] == 'game']}
                        for region, names in kit['sets'].items()},
            'platforms': list(PLATFORMS),
            'capabilities': {'bound_arcade_inputs': True, 'disc_resource_check': True,
                             'collection_export': False}}
