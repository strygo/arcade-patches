"""The content audit rejects new names and review citations in downloads."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import content_audit


def kit(folder: Path, lines: list[str]) -> Path:
    path = folder / "demo-rc1-ips.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "\n".join(lines))
    return path


class ContentAuditTests(unittest.TestCase):
    def test_author_credit_passes_and_citations_fail(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(content_audit, "BASELINE", Path(tmp) / "none.json"):
            content_audit.check(kit(Path(tmp), ["Patch by: Steve Gordon (https://x.com/strygo)"]))
            for line in ("# the block reported in review", "# the user asked for this",
                         "# ear-approved seam", "see /Users/someone/x", "Steve liked it"):
                with self.assertRaises(ValueError, msg=line):
                    content_audit.check(kit(Path(tmp), [line]))

    def test_baseline_tolerates_already_public_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "baseline.json"
            base.write_text('{"tolerated": [{"member": "readme.txt", "line": "# ear-approved seam"}]}')
            with patch.object(content_audit, "BASELINE", base):
                content_audit.check(kit(Path(tmp), ["# ear-approved seam"]))
                with self.assertRaises(ValueError):
                    content_audit.check(kit(Path(tmp), ["# ear-approved seam", "# user-reported glitch"]))


if __name__ == "__main__":
    unittest.main()
