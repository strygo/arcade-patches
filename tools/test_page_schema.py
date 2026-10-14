import copy
import json
import unittest
from pathlib import Path

import page_schema

ROOT = Path(__file__).resolve().parents[1]


def page(**extra):
    base = {"slug": "demo", "title": "Demo", "subtitle": "Enhanced version", "status": "release-candidate",
            "version": "rc2", "date": "2026-10-10", "game": "Demo", "hardware": "Fixture",
            "summary": "Demo.", "description": ["Demo."], "changes": ["A change"],
            "screenshots": [{"single": "a.png", "caption": "A."}],
            "release_history": [{"version": "rc2", "date": "2026-10-10", "items": ["Fixed."]},
                                {"version": "rc1", "date": "2026-09-01", "items": ["First."]}]}
    base.update(extra)
    return base


class PageSchemaTests(unittest.TestCase):
    def test_current_pages_pass(self):
        page_schema.validate(json.loads((ROOT / "data/patches.json").read_text()))

    def test_valid_page(self):
        self.assertEqual([], page_schema.problems(page()))

    def test_history_needs_items_even_on_hidden_pages(self):
        p = page(hidden=True)
        p["release_history"][0] = {"version": "rc2", "date": "2026-10-10", "changes": ["Fixed."]}
        self.assertTrue(any("exactly version, date and items" in m for m in page_schema.problems(p)))

    def test_newest_history_matches_page(self):
        p = page(version="rc3")
        self.assertTrue(any("newest release_history" in m for m in page_schema.problems(p)))

    def test_visible_page_needs_screenshots_hidden_may_wait(self):
        self.assertTrue(page_schema.problems(page(screenshots=[])))
        self.assertEqual([], page_schema.problems(page(screenshots=[], hidden=True)))

    def test_in_development_page_has_no_version_yet(self):
        p = page(status="in-development", version=None, date=None, hidden=True)
        del p["release_history"]
        self.assertEqual([], page_schema.problems(p))
        self.assertTrue(page_schema.problems(page(version=None)))

    def test_screenshot_needs_caption_and_image(self):
        self.assertTrue(page_schema.problems(page(screenshots=[{"single": "a.png"}])))
        self.assertTrue(page_schema.problems(page(screenshots=[{"before": "a.png", "caption": "A."}])))

    def test_builds_need_every_field_the_renderer_reads(self):
        real = "data/screenshots/vsav2-arrange/title_us.png"
        build = {"key": "us", "region": "USA", "title": "Demo", "hbmame_set": "demo01", "title_screen": real}
        self.assertEqual([], page_schema.problems(page(builds=[build])))
        for missing in page_schema.BUILD_KEYS:
            incomplete = {k: v for k, v in build.items() if k != missing}
            problems = page_schema.problems(page(builds=[incomplete], hidden=True))
            self.assertTrue(any(f"missing {missing}" in m for m in problems), missing)

    def test_build_title_screen_must_exist_and_keys_be_distinct(self):
        build = {"key": "us", "region": "USA", "title": "Demo", "hbmame_set": "demo01",
                 "title_screen": "data/screenshots/demo/nope.png"}
        self.assertTrue(any("does not exist" in m for m in page_schema.problems(page(builds=[build], hidden=True))))
        real = dict(build, title_screen="data/screenshots/vsav2-arrange/title_us.png")
        self.assertTrue(any("distinct keys" in m for m in page_schema.problems(page(builds=[real, dict(real)]))))
        self.assertTrue(page_schema.problems(page(builds=[])))

    def test_validate_raises_with_every_problem(self):
        config = {"patches": [page(date="10/10/2026"), page(slug="other", changes="x")], "projects": []}
        with self.assertRaises(page_schema.PageSchemaError) as raised:
            page_schema.validate(config)
        self.assertIn("demo: date", str(raised.exception))
        self.assertIn("other: changes", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
