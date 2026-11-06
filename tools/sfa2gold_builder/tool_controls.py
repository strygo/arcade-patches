"""Shared command-line cache/offline controls for standalone recipient tools."""
import argparse
import os
from pathlib import Path
import shutil
import hashlib
from pathlib import PurePosixPath


def verify_required_files(root, files):
    """Verify declared software hashes without traversing unrelated user data."""
    root = Path(root)
    for name, expected in files.items():
        if not isinstance(name,str) or not name or '\\' in name or ':' in name or PurePosixPath(name).is_absolute() or any(p in ('','.','..') for p in name.split('/')):
            raise ValueError('Unsafe kit file path: '+str(name))
        path=root
        for part in PurePosixPath(name).parts:
            path /= part
            if path.is_symlink():
                raise ValueError('Required kit file must not use symlinks: '+name)
        if not path.is_file():
            raise ValueError('Missing kit file: '+name+'. Restore it from the original ZIP.')
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024**2),b''):digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError('Modified kit file: '+name+'. Restore it from the original ZIP.')


def executable_path(value, names, *, label='Tool'):
    """Accept an executable, its containing folder, or a command on PATH."""
    path = Path(value).expanduser()
    if path.is_dir():
        ordered = sorted(names,key=lambda n:n.lower().endswith('.exe') != (os.name == 'nt'))
        candidates = [path/name for name in ordered]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            raise ValueError(label+' folder contains none of: '+', '.join(names)+': '+str(value))
    elif not path.is_file():
        found = shutil.which(str(path))
        if not found:
            raise ValueError(label+' not found: '+str(value)+'. Supply its executable, containing folder, or command on PATH.')
        path = Path(found)
    if os.name != 'nt' and not os.access(path,os.X_OK):
        raise ValueError(label+' is not executable: '+str(path))
    return path.resolve()


def add_arguments(parser):
    parser.add_argument('--tools-cache', type=Path, help='verified media/helper cache')
    parser.add_argument('--tools-bundle', type=Path, help='offline tools ZIP or directory')
    parser.add_argument('--offline', action='store_true', help='never download tools')


def configure(argv):
    parser = argparse.ArgumentParser(add_help=False)
    add_arguments(parser)
    args, rest = parser.parse_known_args(argv)
    for option, key in (('tools_cache', 'CPSPLUS_SWITCH_TOOLS_CACHE'),
                        ('tools_bundle', 'CPSPLUS_SWITCH_TOOLS_BUNDLE')):
        value = getattr(args, option)
        if value is not None:
            os.environ[key] = str(value.expanduser().resolve())
    if args.offline:
        os.environ['CPSPLUS_SWITCH_TOOLS_OFFLINE'] = '1'
    return rest
