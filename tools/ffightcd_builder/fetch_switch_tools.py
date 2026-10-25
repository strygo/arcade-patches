#!/usr/bin/env python3
"""Obtain content-pinned Switch helpers without a compiler (standard library only)."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024


class SetupError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SetupError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def host_id():
    machine = platform.machine().lower()
    arch = {'amd64':'x86_64', 'x64':'x86_64', 'aarch64':'arm64'}.get(machine,machine)
    system = {'Darwin':'macos','Windows':'windows','Linux':'linux'}.get(platform.system())
    require(system and arch in ('arm64','x86_64'), 'Unsupported helper host: '+platform.platform())
    return system+'-'+arch


def default_cache():
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))
    elif platform.system() == 'Darwin':
        base = Path.home()/'Library/Caches'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME',Path.home()/'.cache'))
    return base/'capcom/switch-helpers'


def read_lock(path=None):
    lock = json.loads(Path(path or HERE/'switch_helpers.json').read_text())
    require(lock['schema'] == 1 and lock['platforms'], 'Invalid helper manifest')
    return lock


def safe_name(name):
    p = PurePosixPath(name)
    require(name and '\\' not in name and ':' not in name and not p.is_absolute()
            and all(x not in ('','.','..') for x in name.split('/')), 'Unsafe helper member: '+name)
    return name


def inventory(data, record):
    require(len(data) == record['bytes'] and sha(data) == record['sha256'],
            'Helper archive hash/size mismatch; nothing will be executed')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        require(len(infos) == len({i.filename for i in infos}), 'Duplicate helper member')
        require(sum(i.file_size for i in infos) <= MAX_EXPANDED, 'Helper archive is too large')
        files = {}
        for info in infos:
            safe_name(info.filename)
            require(not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16),
                    'Invalid helper member type')
            files[info.filename] = z.read(info)
    require(set(files) == set(record['files']), 'Helper archive inventory differs')
    for name,data in files.items():
        require(sha(data) == record['files'][name], 'Helper member hash mismatch: '+name)
    return files


def checked_cache(root, record):
    expected = set(record['files'])
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    require(actual == expected and not root.is_symlink(),
            'Helper cache inventory changed; remove this cache directory: '+str(root))
    for name,digest in record['files'].items():
        p = root/safe_name(name)
        chain = [p]
        while chain[-1] != root:
            chain.append(chain[-1].parent)
        require(not any(q.is_symlink() for q in chain)
                and p.is_file() and sha(p.read_bytes()) == digest,
                'Helper cache changed; remove this cache directory: '+str(root))
    return {key:root/safe_name(name) for key,name in record['executables'].items()}


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require(urllib.parse.urlparse(newurl).scheme == 'https', 'Insecure helper redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, size):
    require(urllib.parse.urlparse(url).scheme == 'https' and 0 < size <= MAX_ARCHIVE,
            'Helpers require HTTPS and a bounded pinned size')
    with urllib.request.build_opener(HTTPSOnlyRedirect()).open(url, timeout=60) as response:
        require(urllib.parse.urlparse(response.geturl()).scheme == 'https', 'Insecure helper redirect')
        data = response.read(size+1)
    require(len(data) == size, 'Helper download size differs')
    return data


def bundle_archive(path, record):
    path = Path(path).expanduser()
    if path.is_dir():
        path = path/record['filename']
    require(path.is_file(), 'Offline tools bundle is missing: '+str(path))
    require(path.stat().st_size <= 256*1024*1024, 'Offline tools bundle is too large')
    data = path.read_bytes()
    # A per-host archive, or the all-host offline archive containing it.
    if sha(data) == record['sha256']:
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        matches = [i for i in z.infolist() if i.filename == record['filename']]
        require(len(matches) == 1 and matches[0].file_size == record['bytes'],
                'Offline bundle lacks the required pinned host archive: '+record['filename'])
        require(matches[0].file_size <= MAX_ARCHIVE, 'Offline helper archive is too large')
        return z.read(matches[0])


def setup(root=None, *, manifest=None, host=None):
    # root is accepted for the existing source-tool API; durable user cache is
    # intentionally independent of disposable per-build work directories.
    lock = read_lock(manifest)
    host = host or host_id()
    require(host in lock['platforms'], 'No qualified prebuilt helpers for '+host)
    record = lock['platforms'][host]
    require(set(record['executables']) == {'nstool','decoder'}
            and len(set(record['executables'].values())) == 2
            and set(record['executables'].values()) <= set(record['files']), 'Incomplete helper pair')
    return obtain(record, host, label='Switch helpers')


def obtain(record, host, *, label):
    """Install one independently pinned tool archive using the common controls."""
    if os.name == 'nt' and record.get('minimum_windows_build'):
        require(sys.getwindowsversion().build >= record['minimum_windows_build'],
                'This helper bundle requires Windows 11 x64 emulation')
    require(record['executables'] and set(record['executables'].values()) <= set(record['files']),
            'Incomplete helper executables')
    cache = Path(os.environ.get('CPSPLUS_SWITCH_TOOLS_CACHE') or default_cache()).expanduser()
    target = cache/host/record['sha256']
    if target.exists():
        return checked_cache(target,record)
    bundle = os.environ.get('CPSPLUS_SWITCH_TOOLS_BUNDLE')
    offline = os.environ.get('CPSPLUS_SWITCH_TOOLS_OFFLINE','').lower() in ('1','true','yes')
    require(bundle or not offline, 'Required helpers are not cached. Supply --tools-bundle for offline use.')
    data = bundle_archive(bundle,record) if bundle else download(record['url'],record['bytes'])
    files = inventory(data,record)
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.helpers-',dir=target.parent) as temporary:
        stage = Path(temporary)/'tools';stage.mkdir()
        for name,data in files.items():
            path = stage/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        for name in record['executables'].values():
            (stage/name).chmod(0o755)
        try:
            stage.rename(target)
        except OSError:
            # Another verified installer may have completed the same cache.
            if not target.is_dir():
                raise
    tools = checked_cache(target,record)
    print('Ready: verified prebuilt '+label+' ('+host+')',flush=True)
    return tools
