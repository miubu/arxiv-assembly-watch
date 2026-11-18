"""Fetch a version-pinned arXiv paper and Zotero metadata for full-text review."""
import argparse
from datetime import date
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request


class Citation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            values = dict(attrs)
            name = values.get("name", "")
            if name.startswith("citation_"):
                self.fields.setdefault(name[9:], []).append(values["content"])


def fetch(path):
    request = urllib.request.Request("https://arxiv.org/" + path,
                                     headers={"User-Agent": "arxiv-assembly-watch/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def write_atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def prepare(paper_id, root):
    match = re.fullmatch(r"(\d{4}\.\d{4,5})v[1-9]\d*", paper_id)
    if not match:
        raise ValueError("Pass a versioned modern arXiv ID, e.g. 2611.20984v2")
    folder = root / paper_id
    folder.mkdir(parents=True, exist_ok=True)
    # Subsequent runs keep the reviewer's note in this file.
    record = folder / "paper.json"
    if not record.exists():
        citation = Citation()
        citation.feed(fetch("abs/" + paper_id).decode())
        fields = citation.fields
        if fields["arxiv_id"] != [match[1]]:
            raise ValueError("arXiv returned a different paper")
        authors = [" ".join(reversed(a.split(", ", 1))) for a in fields["author"]]
        entry = {"id": paper_id, "title": fields["title"][0], "authors": authors,
                 "published": date.fromisoformat(fields["date"][0].replace("/", "-")).isoformat(),
                 "summary": fields["abstract"][0], "note": ""}
        write_atomic(record, (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode())
    pdf = folder / (paper_id + ".pdf")
    if not pdf.exists():
        data = fetch("pdf/" + paper_id)
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            raise ValueError("arXiv did not return a complete PDF")
        write_atomic(pdf, data)
    return folder


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper_id")
    parser.add_argument("--root", type=Path, default=Path(".arxiv-watch/papers"))
    args = parser.parse_args()
    print(prepare(args.paper_id, args.root))
