"""Tools section and portable launcher page, separate from game release pages."""
import json
import re
from html import escape as esc
from tool_inventory import validate


def load(root):
    source = root / 'data/tools.json'
    if not source.exists():
        return None
    entry = json.loads(source.read_text())
    record = validate(root, entry)
    return (entry, record) if not entry.get('hidden') else None


def card(entry, prefix=''):
    return f'''<a class="card tool" href="{prefix}arcadepatches/">
<div><h2>{esc(entry['title'])}</h2><div class="sub">Collection builder · v{esc(entry['version'])}</div>
<p class="summary">{esc(entry['summary'])}</p></div>
<img class="thumb" src="{prefix}img/arcadepatches/00_single.png" alt="ArcadePatches terminal menu"></a>'''


def section(entry):
    return f'''<section class="kind" id="tools"><h2 class="kind"><a href="tools/">Tools</a></h2>
<p class="kind-intro">Build and manage your collection from media you own.</p>{card(entry)}</section>'''


def render(builder, site, loaded):
    entry, record = loaded
    shots = builder.copy_screenshots(entry)
    def paragraphs(values):
        return ''.join(f'<p>{esc(value)}</p>' for value in values)
    features = '<ul>' + ''.join(f'<li>{esc(p)}</li>' for p in entry['features']) + '</ul>'
    steps = '<ol>' + ''.join(f'<li>{esc(p)}</li>' for p in entry['steps']) + '</ol>'
    screenshots = builder.render_shots(shots, 'Inside the terminal')
    screenshots = re.sub(r'<img src="([^"]+)"([^>]+)>', r'<a href="\1"><img src="\1"\2></a>', screenshots)
    archive = record['launcher']
    spec = record['files'][archive]
    body = f'''<a class="back" href="../tools/">← Tools</a>
<h1>{esc(entry['title'])}</h1><p class="subtitle">Collection builder · v{esc(entry['version'])}</p>
<p>{esc(entry['summary'])}</p>
<div class="download"><div class="kind">Python launcher · Windows, macOS and Linux</div>
<div class="file"><a href="../{archive}">Download ArcadePatches {esc(entry['version'])}</a> ({builder.human_size(spec['size'])})</div>
<div class="hash">SHA-256: {spec['sha256']}</div></div>
<p>Free download. Python is required. No ROMs or game data are included.</p>
<h2>What it does</h2>{features}
<div class="tool-shots">{screenshots}</div>
<p>Open a screenshot to view it at full size.</p>
<h2>Before you start</h2>{paragraphs(entry['requirements'])}
<h2>Get started</h2>{steps}
<pre><code># Windows\npy -3.13 arcadepatches.py\n\n# Apple Silicon Mac\npython3.12 arcadepatches.py\n\n# Linux\npython3 arcadepatches.py</code></pre>
<p>The included QUICKSTART.txt covers command-line filters, offline updates, cache limits and recovery.</p>
<h2>Updates and output</h2>
<p>At startup, the launcher checks for new games, versions and changed requirements without scanning your ROM folders. Choose Build (shown as Update after your first completed build) to check your media and build the ready games. Keeping “all” selected includes future matching games, platforms and regions. It does not run in the background.</p>
<p>Copying to MiSTer, HBMAME or another destination is manual. Some soundtrack exports are overlays that need the named game files already installed; follow INSTALL.txt. Changing a selection keeps existing exports.</p>
<p>Prefer to build one game directly? Each game's page still has its standalone download and instructions.</p>'''
    image = 'img/arcadepatches/00_single.png'
    social = {'title': entry['title'], 'description': entry['summary'], 'path': 'arcadepatches/', 'image': image}
    directory = builder.DOCS / 'arcadepatches'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'index.html').write_text(builder.page(site, 'ArcadePatches · Tools', body, depth=1, social=social))
    directory = builder.DOCS / 'tools'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'index.html').write_text(builder.page(site, 'Tools · Arcade Patches',
        '<h1>Tools</h1><p>Build and manage your collection from media you own.</p>' + card(entry, '../'), depth=1,
        social=dict(social, title='Tools · Arcade Patches', path='tools/')))
