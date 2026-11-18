#!/usr/bin/env python3
"""Translate a local PDF with the active Zotero PDF2zh settings and resume on rerun."""

import argparse
import base64
import configparser
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
import zipfile

# getServerConfig() in the installed Zotero PDF2zh 4.1.8 plugin converts booleans and numbers to strings.
FIELDS = """service next_service engine sourceLang targetLang skipLastPages threadNum qps poolSize
mono dual compare babeldoc skipSubsetFonts fontFile fontFamily dualMode transFirst ocr autoOcr noWatermark
saveGlossary disableGlossary noDual noMono skipClean disableRichTextTranslate enhanceCompatibility
translateTableText onlyIncludeTranslatedPage""".split()
PREF = re.compile(r'^(?:user_)?pref\("extensions\.zotero\.pdf2zh\.([^"]+)",\s*(.+)\);$')


def zotero_profile():
    appdata = os.environ.get("APPDATA")
    if os.name != "nt":
        if not appdata:
            appdata = subprocess.check_output(["cmd.exe", "/d", "/c", "echo", "%APPDATA%"]).decode().strip()
        appdata = subprocess.check_output(["wslpath", "-u", appdata], text=True).strip()
    root = Path(appdata) / "Zotero" / "Zotero"
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(root / "profiles.ini", encoding="utf-8")
    for section in ini.sections():
        if section.startswith("Profile") and ini[section].get("Default") == "1":
            value = ini[section]["Path"]
            if ini[section].get("IsRelative") == "1":
                return root / value
            if os.name != "nt":
                value = subprocess.check_output(["wslpath", "-u", value], text=True).strip()
            return Path(value)
    raise ValueError("Could not identify the default Zotero profile in profiles.ini")


def configuration(profile):
    xpi = profile / "extensions" / "pdf2zh@guaguastandup.com.xpi"
    with zipfile.ZipFile(xpi) as archive:
        defaults = archive.read("prefs.js").decode()
        version = json.loads(archive.read("manifest.json"))["version"]
    prefs = {}
    for source in (defaults, (profile / "prefs.js").read_text(encoding="utf-8")):
        for line in source.splitlines():
            match = PREF.fullmatch(line)
            if match:
                prefs[match[1]] = json.loads(match[2])
    keys = {k: k for k in FIELDS}
    keys.update(serverUrl="new_serverip", mono_cut="mono-cut", dual_cut="dual-cut", crop_compare="crop-compare")
    config = {k: json.dumps(prefs[v]) if isinstance(prefs[v], bool) else str(prefs[v]) for k, v in keys.items()}
    base = config["serverUrl"].strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Invalid server address in Zotero PDF2zh preferences")
    if not config["targetLang"].lower().replace("_", "-").startswith("zh"):
        raise ValueError("Select Chinese as the target language in Zotero PDF2zh")
    config["serverUrl"] = base
    service = config["service" if config["engine"] == "pdf2zh" else "next_service"]
    active = next((api for api in json.loads(prefs.get("llmApis", "[]"))
                   if api.get("key") and api.get("activate") and api.get("service") == service), None)
    if active:
        config["llm_api"] = {k: active[k] for k in ("service", "model", "apiKey", "apiUrl") if k in active}
        config["llm_api"]["extraData"] = active.get("extraData") or {}
    return config, version


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Backend:
    def __init__(self, base, version):
        self.base = base
        self.version = version
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None):
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers.update({"Content-Type": "application/json", "X-PDF2zh-Protocol": "accepted",
                            "X-PDF2zh-Plugin-Version": self.version})
        return self.opener.open(Request(self.base + path, data=body, headers=headers), timeout=45)

    def data(self, path, payload=None):
        with self.request(path, payload) as response:
            return json.load(response)

    def task(self, task_id, name):
        for table in ("tasks", "history"):
            records = self.data("/api/" + table)[table]
            for record in records:
                rid = str(record.get("taskId") or record.get("task_id") or "")
                if (task_id and rid == task_id) or (not task_id and rid and record.get("fileName") == name):
                    return rid, record
        return "", {}


def file_variant(result, config):
    files = result.get("fileList") or []
    if not files:
        files = [re.split(r"[/\\]", path)[-1] for path in (result.get("filePaths") or [])]
    variants = ["mono", "dual", "mono-cut", "dual-cut", "crop-compare", "compare"]
    variants.sort(key=lambda variant: config[variant.replace("-", "_")] != "true")
    for variant in variants:
        for name in files:
            if (isinstance(name, str) and re.search(r"[._-]" + re.escape(variant) + r"\.pdf$", name, re.I)
                    and not re.search(r"[/\\\x00-\x1f]", name)):
                return name
    return ""


def pdf_hash(path):
    with path.open("rb") as f:
        if b"%PDF-" not in f.read(1024):
            raise ValueError("Expected a PDF header")
        f.seek(max(0, f.seek(0, os.SEEK_END) - 4096))
        if b"%%EOF" not in f.read():
            raise ValueError("Incomplete PDF")
        f.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def remember(db, key, **fields):
    with db:
        db.execute("UPDATE jobs SET " + ", ".join(f"{field}=?" for field in fields) + " WHERE id=?",
                   (*fields.values(), key))


def translate(source, output, config, backend, db, wait, retry_unconfirmed=False):
    if source.resolve() == output.resolve():
        raise ValueError("Choose a separate path for the translated PDF")
    digest = pdf_hash(source)
    identity = [digest, config["serverUrl"], config["sourceLang"], config["targetLang"]]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    filename = f"arxiv-watch-{key[:32]}.pdf"
    db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, task TEXT, result TEXT, sha TEXT)")
    row = db.execute("SELECT task, result, sha FROM jobs WHERE id=?", (key,)).fetchone()
    if output.exists():
        if row and row[2] and pdf_hash(output) == row[2]:
            return
        raise ValueError("Output exists and does not match a recorded translation")
    if not row:
        # Check reachability before marking submission uncertain.
        backend.data("/api/tasks")
        with db:
            new = db.execute("INSERT OR IGNORE INTO jobs(id) VALUES(?)", (key,)).rowcount
        row = db.execute("SELECT task, result, sha FROM jobs WHERE id=?", (key,)).fetchone()
    else:
        new = False
    task_id, result_name, _ = row
    if not new and not task_id and not result_name:
        task_id, _ = backend.task(None, filename)
        if task_id:
            remember(db, key, task=task_id)
        elif retry_unconfirmed:
            # Only an explicit operator check may re-submit a connection-ambiguous POST.
            new = True
        else:
            raise RuntimeError(f"Submission uncertain for {filename}. Check PDF2zh history before --retry-unconfirmed.")
    if new:
        payload = dict(config, fileName=filename, asyncJob=True,
                       fileContent="data:application/pdf;base64," + base64.b64encode(source.read_bytes()).decode("ascii"))
        try:
            result = backend.data("/translate", payload)
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (408, 409):
                with db:
                    db.execute("DELETE FROM jobs WHERE id=?", (key,))
            raise
        task_id = str(result.get("taskId") or result.get("task_id") or "")
        result_name = file_variant(result, config) if result.get("status") == "success" else ""
        if not (task_id or result_name):
            raise RuntimeError("Backend did not return a confirmed task ID or output. Check PDF2zh history.")
        remember(db, key, task=task_id, result=result_name)
    deadline = time.monotonic() + wait
    while not result_name:
        _, record = backend.task(task_id, filename)
        nested = record.get("result") or {}
        statuses = [record.get("status"), nested.get("status")]
        if any(s in ("error", "failed", "失败") for s in statuses):
            with db:
                db.execute("DELETE FROM jobs WHERE id=?", (key,))
            raise RuntimeError(f"PDF2zh task {task_id} failed. Check the backend log before retrying.")
        if record.get("finished") or any(s in ("success", "完成") for s in statuses):
            result_name = file_variant(nested, config) or file_variant(record, config)
            if result_name:
                remember(db, key, result=result_name)
                break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"PDF2zh task {task_id} is pending. Rerun the same command to resume.")
        time.sleep(min(4, max(0, deadline - time.monotonic())))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with backend.request("/translatedFile/" + quote(result_name, safe="")) as response, tmp_path.open("wb") as f:
            shutil.copyfileobj(response, f)
        checksum = pdf_hash(tmp_path)
        remember(db, key, sha=checksum)
        try:
            os.link(tmp_path, output)
        except FileExistsError:
            if pdf_hash(output) != checksum:
                raise ValueError("Output path is occupied by another PDF") from None
    finally:
        tmp_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--retry-unconfirmed", action="store_true",
                        help="Only after checking the backend has no job for this input")
    args = parser.parse_args()
    config, version = configuration(zotero_profile())
    state = Path.cwd() / ".arxiv-watch"
    state.mkdir(exist_ok=True)
    with closing(sqlite3.connect(state / "translations.sqlite3", timeout=30)) as db:
        translate(args.source, args.output, config, Backend(config["serverUrl"], version),
                  db, args.wait_seconds, args.retry_unconfirmed)
    print(args.output)


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        sys.exit(f"PDF2zh HTTP {error.code}; inspect the configured backend")
    except URLError:
        sys.exit("Could not reach the configured PDF2zh backend")
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile) as error:
        sys.exit(str(error))
