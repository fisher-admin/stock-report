import json
import tempfile
import unittest
from pathlib import Path

from orchestrator_common import write_json


class AtomicJsonStateTests(unittest.TestCase):
    def test_shared_json_writer_replaces_symlink_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "target.json"
            link = root / "state.json"
            target.write_text('{"safe": true}', encoding="utf-8")
            link.symlink_to(target)
            write_json(link, {"ok": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"safe": True})
            self.assertFalse(link.is_symlink())
            self.assertEqual(json.loads(link.read_text(encoding="utf-8")), {"ok": True})


if __name__ == "__main__":
    unittest.main()
