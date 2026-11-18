import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.prepare import prepare


class PrepareTest(unittest.TestCase):
    def test_retry_preserves_review_and_recovers_interrupted_pdf(self):
        citation = b'''<meta name="citation_arxiv_id" content="2611.20984">
<meta name="citation_title" content="Tools &amp; Planning">
<meta name="citation_author" content="Wang, Ruizhe">
<meta name="citation_date" content="2026/11/16">
<meta name="citation_abstract" content="Assembly planning.">'''
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = "2611.20984v2"
            with patch("scripts.prepare.fetch", side_effect=[citation, b"partial"]):
                with self.assertRaises(ValueError):
                    prepare(paper, root)
            record = root / paper / "paper.json"
            entry = json.loads(record.read_text())
            self.assertEqual(entry["authors"], ["Ruizhe Wang"])
            self.assertEqual(entry["title"], "Tools & Planning")
            entry["note"] = "reviewed"
            record.write_text(json.dumps(entry))
            with patch("scripts.prepare.fetch", return_value=b"%PDF-1.7\n%%EOF") as fetch:
                prepare(paper, root)
                prepare(paper, root)
                fetch.assert_called_once_with("pdf/" + paper)
            self.assertEqual(json.loads(record.read_text())["note"], "reviewed")
