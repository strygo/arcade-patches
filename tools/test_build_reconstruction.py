"""A partial title gallery must not duplicate or truncate the release build list."""
import json
import copy
import unittest
from pathlib import Path

import build


class ReconstructionTests(unittest.TestCase):
    def test_fbneo_sets_follow_the_published_kit_and_keep_distinct_aliases(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / 'data/patches.json').read_text())
        inventory = build.load_inventory(root / 'data/releases.json')
        for slug, aliases in [('sfa-ex', ['sfaex', 'sfzexj', 'sfzexa']),
                              ('ssf2-ex', ['ssf2tuc', 'ssf2xucj'])]:
            with self.subTest(slug=slug):
                patch = next(p for p in config['patches'] if p['slug'] == slug)
                bundle = build.published_bundle(root, patch, inventory)
                html = build.render_rom_download(patch, bundle)
                self.assertIn('<th>FBNeo set</th>', html)
                self.assertIn('<strong>FBNeo.</strong>', html)
                self.assertIn('--platform fbneo --out-dir out', html)
                self.assertIn('definitions matching this game release', html)
                for alias in aliases:
                    self.assertIn(f'out/fbneo/{alias}.zip', html)
                old_bundle = copy.deepcopy(bundle)
                for variant in old_bundle['variants']:
                    variant.pop('fbneo', None)
                # A prepared page declaration must not advertise an output
                # that the actual published download does not yet contain.
                old_html = build.render_rom_download(patch, old_bundle)
                self.assertNotIn('FBNeo', old_html)

    def test_sfa_ex_lists_all_regions_without_an_asia_title_image(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "data/patches.json").read_text())
        patch = next(p for p in config["patches"] if p["slug"] == "sfa-ex")
        builds = [dict(b, img=f"../img/sfa-ex/title_{b['key']}.png")
                  for b in patch["builds"]]
        bundle = build.published_bundle(root, patch, build.load_inventory(root / "data/releases.json"))
        html = build.render_builds_page(config["site"], patch, builds, bundle)
        gallery = html.split('<div class="build-gallery">', 1)[1].split('</div>', 1)[0]
        self.assertEqual(2, gallery.count('<figure class="build">'))
        self.assertNotIn('Asia', gallery)
        tables = html.split('<h3>The builds</h3>')[1:]
        self.assertEqual(1, len(tables))
        for section in tables:
            table = section.split('</table>', 1)[0]
            for region in ('USA', 'Japan', 'Asia'):
                self.assertIn(region, table)
            self.assertIn('sfzexa', table)


if __name__ == "__main__":
    unittest.main()
