"""Offline checks for idempotent imports, interrupted uploads and key scoping."""
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from zotero import Zotero, attach, has_id, key_for, parent


class ZoteroImportTest(unittest.TestCase):
    @patch("zotero.os.name", "nt")
    @patch("zotero.time.sleep")
    @patch("zotero.open_zotero")
    def test_closed_windows_client_is_launched_and_retried(self, launch, sleep):
        responses = [ConnectionRefusedError(), ConnectionRefusedError(),
                     (b"", {"Zotero-Server-ID": "instance"})]
        with patch.object(Zotero, "request", side_effect=responses):
            self.assertEqual(Zotero().server, "instance")
        launch.assert_called_once()
        self.assertEqual(sleep.call_count, 2)

    def test_parent_with_existing_arxiv_id_retains_library_metadata(self):
        api = Mock()
        api.obj.return_value = None
        existing = {"key": "ABCD2345", "url": "https://arxiv.org/abs/2601.01234v1",
                    "collections": ["OLD"], "version": 8}
        api.request.return_value = ([{"data": existing}], {})
        self.assertEqual(parent(api, {}, "2601.01234", "NEW"), existing["key"])
        api.create.assert_not_called()
        api.request.assert_called_with("PATCH", "/users/0/items/ABCD2345",
                                     {"version": 8, "collections": ["OLD", "NEW"]}, auth=True)
        self.assertFalse(has_id(existing, "2601.0123"))

    def test_empty_attachment_resumes_and_existing_bytes_are_preserved(self):
        api = Mock()
        aid = "2601.01234v1"
        filename = aid + ".pdf"
        att = {"parentItem": "P", "itemType": "attachment", "key": "K", "filename": filename}
        blob = b"mock pdf"
        digest = hashlib.md5(blob).hexdigest()
        path = Mock()
        path.stat.return_value.st_mtime_ns = 1_800_000_000_000_000_000
        api.obj.side_effect = [att, dict(att, md5=digest)]
        api.request.side_effect = [({"uploadKey": "Ab123"}, {}), (b"", {}), (b"", {})]
        self.assertEqual(attach(api, "P", aid, "original", path, blob), "K")
        self.assertEqual(api.request.call_count, 3)
        api.create.assert_not_called()
        api.reset_mock()
        api.obj.side_effect = [dict(att, md5=digest), dict(att, md5="existing")]
        self.assertEqual(attach(api, "P", aid, "original", path, blob), "K")
        with self.assertRaisesRegex(RuntimeError, "preserved"):
            attach(api, "P", aid, "original", path, blob)
        api.request.assert_not_called()

    def test_new_revisions_get_distinct_child_keys(self):
        self.assertNotEqual(key_for("p", "2601.01234v1", "zh"), key_for("p", "2601.01234v2", "zh"))


if __name__ == "__main__":
    unittest.main()
