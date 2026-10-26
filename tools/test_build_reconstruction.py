"""A partial title gallery must not duplicate or truncate the release build list."""
import json
import unittest
from pathlib import Path

import build


class ReconstructionTests(unittest.TestCase):
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
