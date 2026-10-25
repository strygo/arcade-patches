"""Load pinned NumPy/Pillow wheels privately, without pip or global installation.

This is a deliberately restricted wheel reader, not a general package manager.
Only the flat, dependency-free wheels in the shipped lock are supported. The
complete wheel is authenticated before extraction; cached files are checked
against that authenticated archive before being loaded in a new process.
"""
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import struct
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
MODULES = {'numpy': 'numpy', 'PIL': 'Pillow'}


class DependencyError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise DependencyError(message)


def host_id():
    # platform.machine() can describe the ARM Windows host, rather than the
    # x64 interpreter running under emulation. Wheel ABI follows Python.
    machine = sysconfig.get_platform().lower()
    if sys.platform == 'win32':
        host = 'windows-' + ('x86_64' if machine == 'win-amd64' else 'arm64' if machine == 'win-arm64' else 'x86')
    elif sys.platform == 'darwin':
        host = 'macos-' + ('arm64' if platform.machine() == 'arm64' else 'x86_64')
    else:
        host = 'linux-' + platform.machine().lower()
    abi = 'cp' + str(sys.version_info.major) + str(sys.version_info.minor)
    if sysconfig.get_config_var('Py_GIL_DISABLED'):
        abi += 't'
    if sys.implementation.name != 'cpython' or struct.calcsize('P') != 8:
        abi = sys.implementation.name + '-' + abi
    return host + '-' + abi


def cache_root():
    override = os.environ.get('ARCADE_PYTHON_CACHE')
    if override:
        return Path(override).expanduser().absolute()
    tools = os.environ.get('CPSPLUS_SWITCH_TOOLS_CACHE')
    if tools:
        return Path(tools).expanduser().absolute() / 'python'
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Caches'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'ArcadePatches/python'


def configure(argv):
    """Read our options early, even before a legacy entry point imports PIL."""
    options = {'--python-cache': 'ARCADE_PYTHON_CACHE',
               '--python-wheels': 'ARCADE_PYTHON_WHEELS',
               '--tools-cache': 'CPSPLUS_SWITCH_TOOLS_CACHE',
               '--tools-bundle': 'CPSPLUS_SWITCH_TOOLS_BUNDLE'}
    for i, arg in enumerate(argv):
        key, separator, value = arg.partition('=')
        if key in options:
            if not separator:
                require(i + 1 < len(argv) and not argv[i + 1].startswith('--'), key + ' needs a path')
                value = argv[i + 1]
            os.environ[options[key]] = str(Path(value).expanduser().absolute())
        elif arg == '--offline':
            os.environ['CPSPLUS_SWITCH_TOOLS_OFFLINE'] = '1'


def _offline_bytes(pin):
    # A dedicated wheel bundle can coexist with the binary tools bundle.
    bundles = [os.environ.get('ARCADE_PYTHON_WHEELS'), os.environ.get('CPSPLUS_SWITCH_TOOLS_BUNDLE')]
    for value in filter(None, bundles):
        path = Path(value).expanduser()
        require(path.exists(), 'Offline bundle does not exist: ' + str(path))
        if path.is_dir():
            candidate = path / pin['filename']
            if candidate.is_file():
                require(candidate.stat().st_size == pin['bytes'], 'Offline wheel size differs: ' + candidate.name)
                return candidate.read_bytes()
            # Also accept the unchanged, separate offline wheel ZIPs.
            for candidate in sorted(path.glob('python-wheels-*.zip')):
                with zipfile.ZipFile(candidate) as archive:
                    matches = [i for i in archive.infolist() if i.filename == pin['filename']]
                    if matches:
                        require(len(matches) == 1 and matches[0].file_size == pin['bytes'], 'Invalid offline wheel member')
                        return archive.read(matches[0])
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                matches = [i for i in archive.infolist() if i.filename == pin['filename']]
                if matches:
                    require(len(matches) == 1 and matches[0].file_size == pin['bytes'], 'Invalid offline wheel member')
                    return archive.read(matches[0])
    return None


def wheel_path(pin, root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / pin['filename']
    def authentic(data):
        return len(data) == pin['bytes'] and hashlib.sha256(data).hexdigest() == pin['sha256']
    if path.is_file() and not path.is_symlink() and authentic(path.read_bytes()):
        return path
    data = _offline_bytes(pin)
    if data is None:
        require(os.environ.get('CPSPLUS_SWITCH_TOOLS_OFFLINE', '').lower() not in ('1', 'true', 'yes'),
                'Offline: ' + pin['project'] + ' is not cached. Supply --python-wheels with the matching wheel ZIP or folder.')
        require(pin['url'].startswith('https://files.pythonhosted.org/'), 'Invalid pinned wheel URL')
        print('Downloading ' + pin['project'] + ' ' + pin['version'] + ' for Python (user cache)...', file=sys.stderr, flush=True)
        try:
            with urllib.request.urlopen(pin['url'], timeout=90) as response:
                require(response.geturl().startswith('https://files.pythonhosted.org/'), 'Unexpected wheel redirect')
                data = response.read(pin['bytes'] + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise DependencyError('Could not download ' + pin['project'] + ': ' + str(exc) +
                                  '\nUse --python-wheels with an offline wheel bundle.') from exc
    require(authentic(data), 'Wheel checksum differs: ' + pin['filename'])
    handle, name = tempfile.mkstemp(prefix='.wheel-', dir=root)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def _members(archive):
    result = {}
    for entry in archive.infolist():
        name = entry.filename
        parts = PurePosixPath(name).parts
        require(name and not name.startswith('/') and '\\' not in name and ':' not in name
                and all(p not in ('..', '.') for p in parts), 'Unsafe wheel path: ' + name)
        require(not stat.S_ISLNK(entry.external_attr >> 16), 'Wheel contains a symlink')
        require(not any(p.endswith('.data') for p in parts) and not name.endswith('.pth'),
                'Wheel requires an unsupported installation scheme: ' + name)
        if entry.is_dir():
            continue
        require(name not in result, 'Duplicate wheel member: ' + name)
        result[name] = entry
    wheels = [n for n in result if n.endswith('.dist-info/WHEEL')]
    require(len(wheels) == 1 and b'Wheel-Version: 1.0' in archive.read(wheels[0]), 'Unsupported wheel metadata')
    return result


def _valid(target, archive, members):
    if target.is_symlink() or not target.is_dir():
        return False
    actual = set()
    for path in target.rglob('*'):
        if path.is_symlink():
            return False
        if path.is_file():
            relative = path.relative_to(target).as_posix()
            actual.add(relative)
    if actual != set(members):
        return False
    for name, entry in members.items():
        path = target / name
        if path.stat().st_size != entry.file_size or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(archive.read(entry)).digest():
            return False
    return True


def obtain(project):
    lock = json.loads((HERE / 'wheels.json').read_text(encoding='utf-8'))
    host = host_id()
    require(lock.get('schema') == 1, 'Invalid Python wheel lock')
    require(host in lock['hosts'], 'Automatic ' + project + ' installation is unavailable for ' + host +
            '. Use Python 3.12 ARM64 on Mac or Python 3.13 x64 on Windows, or install the package for your interpreter yourself.')
    pins = [p for p in lock['hosts'][host] if p['project'] == project]
    require(len(pins) == 1, 'Missing pinned wheel: ' + project)
    pin = pins[0]
    root = cache_root() / host
    wheel = wheel_path(pin, root / 'wheels')
    target = root / (project.lower() + '-' + pin['sha256'])
    # Serialize extraction across kits, leaving cached libraries untouched while
    # another Python process may have them loaded (particularly on Windows).
    guard = target.with_suffix('.lock')
    deadline = time.monotonic() + 120
    while True:
        try:
            guard.mkdir()
            break
        except FileExistsError:
            require(time.monotonic() < deadline, 'Python cache is busy: ' + str(guard) +
                    '. If a previous installer was interrupted, close it and remove this lock folder.')
            time.sleep(0.1)
    try:
        with zipfile.ZipFile(wheel) as archive:
            members = _members(archive)
            if not _valid(target, archive, members):
                if target.is_symlink():
                    raise DependencyError('Refusing a symlink in the Python cache: ' + str(target))
                stage = Path(tempfile.mkdtemp(prefix='.python-', dir=root))
                try:
                    for name, entry in members.items():
                        destination = stage / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(archive.read(entry))
                    if target.exists():
                        shutil.rmtree(target)
                    stage.rename(target)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
    finally:
        guard.rmdir()
    return target


def _prepend_env(path):
    value = str(path)
    paths = os.environ.get('PYTHONPATH', '').split(os.pathsep)
    if value not in paths:
        os.environ['PYTHONPATH'] = os.pathsep.join([value] + [p for p in paths if p])


class LazyDependencies(importlib.abc.MetaPathFinder):
    _arcade_python = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in MODULES:
            return None
        # Preserve existing environments and support manually installed packages
        # on hosts outside our explicitly qualified wheel matrix.
        if importlib.machinery.PathFinder.find_spec(fullname, path) is not None:
            return None
        try:
            folder = obtain(MODULES[fullname])
            sys.path.insert(0, str(folder))
            return importlib.machinery.PathFinder.find_spec(fullname, path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise DependencyError('Could not prepare ' + MODULES[fullname] + ': ' + str(exc)) from exc


def bootstrap(argv=None):
    configure(sys.argv[1:] if argv is None else argv)
    # Consume just these shared options, so older kit-specific argument parsers
    # accept them without changing their existing command-line contracts.
    if argv is None:
        rest = [sys.argv[0]]
        skip = False
        for arg in sys.argv[1:]:
            if skip:
                skip = False
                continue
            if arg.partition('=')[0] in ('--python-cache', '--python-wheels'):
                skip = '=' not in arg
                continue
            rest.append(arg)
        sys.argv[:] = rest
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    if not any(getattr(finder, '_arcade_python', False) for finder in sys.meta_path):
        sys.meta_path.insert(0, LazyDependencies())
    # The scope is this installer and its children; no shell profile or global
    # site-packages is changed. A fresh unrelated Python command is unaffected.
    _prepend_env(HERE)
