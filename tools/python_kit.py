"""Export the shared private Python runtime into standalone kit file maps."""
import ast
from pathlib import Path, PurePosixPath
import re

HERE = Path(__file__).resolve().parent
BEGIN = '# BEGIN ARCADE PYTHON BOOTSTRAP'
END = '# END ARCADE PYTHON BOOTSTRAP'


def inject(data, relative):
    text = data.decode('utf-8')
    if BEGIN in text:
        return data
    tree = ast.parse(text)
    after = 0
    for i, node in enumerate(tree.body):
        if (i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)) or isinstance(node, ast.ImportFrom) and node.module == '__future__':
            after = node.end_lineno
        else:
            break
    # Preserve the shebang and encoding declaration even without a docstring.
    if not after:
        for line in text.splitlines()[:2]:
            if line.startswith('#'):
                after += 1
            else:
                break
    depth = len(PurePosixPath(relative).parts) - 1
    block = (BEGIN + '\nimport sys as _arcade_sys\nfrom pathlib import Path as _ArcadePath\n'
             f'_arcade_sys.path.insert(0, str(_ArcadePath(__file__).resolve().parents[{depth}]))\n'
             'from kit_python import bootstrap as _arcade_bootstrap\n_arcade_bootstrap()\n' + END + '\n')
    lines = text.splitlines(keepends=True)
    result = ''.join(lines[:after]) + '\n' + block + ''.join(lines[after:])
    # Prove that only the bootstrap was inserted, retaining the original body.
    assert strip(result).strip() == text.strip()
    ast.parse(result)
    return result.encode('utf-8')


def strip(text):
    return re.sub(r'\n?' + re.escape(BEGIN) + r'\n.*?' + re.escape(END) + r'\n', '', text, flags=re.S)


def overlay(files, entrypoint):
    """Wrap runnable Python scripts; children also inherit the private finder."""
    changed = []
    for name in list(files):
        if name.startswith('kit_python/') or not name.endswith('.py'):
            continue
        text = files[name].decode('utf-8')
        if name != entrypoint and not re.search(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]', text):
            continue
        files[name] = inject(files[name], name)
        changed.append(name)
    for source in sorted((HERE / 'kit_python').iterdir()):
        if source.is_file() and source.suffix in ('.py', '.json'):
            files['kit_python/' + source.name] = source.read_bytes()
    return changed


def copy_runtime(destination):
    import shutil
    target = Path(destination) / 'kit_python'
    target.mkdir(parents=True, exist_ok=True)
    for path in (HERE / 'kit_python').iterdir():
        if path.is_file() and path.suffix in ('.py', '.json'):
            shutil.copyfile(path, target / path.name)
