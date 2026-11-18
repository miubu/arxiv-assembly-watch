"""Run with python3 -B tests/test_translate.py. All backend traffic is mocked."""
import io
from pathlib import Path
import sqlite3
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import translate as t


class TranslationTest(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory(dir=".")
        self.addCleanup(workspace.cleanup)
        self.src = Path(workspace.name) / "test.pdf"
        self.src.write_bytes(b"%PDF-1.4\noriginal\n%%EOF")
        self.dest = self.src.with_name("test.zh.pdf")
        self.translated = b"%PDF-1.4\ntranslated\n%%EOF"
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.cfg = {k: "false" for k in ("mono", "dual", "mono_cut", "dual_cut", "compare", "crop_compare")}
        self.cfg.update(dual="true", serverUrl="http://example.invalid", sourceLang="en", targetLang="zh-CN")
        self.backend = Mock()
        self.backend.data.side_effect = [{"tasks": []}, {"status": "accepted", "taskId": "test-id"}]
        self.backend.request.return_value = io.BytesIO(self.translated)
        self.done = {"finished": True, "status": "success", "result": {"status": "success",
                     "fileList": ["article.mono.pdf", "article.LR-dual.pdf"]}}

    def translate(self):
        t.translate(self.src, self.dest, self.cfg, self.backend, self.db, wait=0)

    def test_confirmed_job_resumes_without_resubmission(self):
        self.backend.task.return_value = ("test-id", {"status": "running"})
        with self.assertRaisesRegex(RuntimeError, "pending"):
            self.translate()
        name, payload = self.backend.data.call_args.args
        self.assertEqual("/translate", name)
        self.assertTrue(payload["asyncJob"])
        self.assertTrue(payload["fileContent"].startswith("data:application/pdf;base64,JVBER"))
        self.backend.task.return_value = ("test-id", self.done)
        self.translate()
        self.translate()
        self.assertEqual(self.translated, self.dest.read_bytes())
        self.backend.request.assert_called_once_with("/translatedFile/article.LR-dual.pdf")
        self.assertEqual(2, self.backend.data.call_count)

    def test_ambiguous_task_recover_by_marked_filename(self):
        self.backend.data.side_effect = [{"tasks": []}, URLError("lost response")]
        with self.assertRaises(URLError):
            self.translate()
        self.backend.task.return_value = ("", {})
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.translate()
        self.assertEqual(self.backend.data.call_args.args[1]["fileName"], self.backend.task.call_args.args[1])
        self.backend.task.side_effect = [("test-id", {}), ("test-id", self.done)]
        self.translate()
        self.assertEqual(self.translated, self.dest.read_bytes())
        self.assertEqual(2, self.backend.data.call_count)

    def test_names_and_download_validation(self):
        self.assertEqual("", t.file_variant({"fileList": ["../fake.dual.pdf", "http://host/file.dual.pdf"]}, self.cfg))
        self.backend.task.return_value = ("test-id", self.done)
        self.backend.request.return_value = io.BytesIO(b"<html>error</html>")
        with self.assertRaisesRegex(ValueError, "PDF header"):
            self.translate()
        self.assertFalse(self.dest.exists())
        self.backend.request.return_value = io.BytesIO(self.translated)
        self.translate()
        self.assertEqual(2, self.backend.data.call_count)

    def test_existing_output_protected(self):
        self.dest.write_bytes(self.translated)
        with self.assertRaisesRegex(ValueError, "Output exists"):
            self.translate()
        self.backend.data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
