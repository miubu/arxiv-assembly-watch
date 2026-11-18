"""Import a reviewed paper and verified PDF files through the Zotero 10 local API."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import html
import http.client
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import shutil
from urllib.parse import urlencode

COLLECTION = "arXiv 每日精选"
LIB = "/users/0/"
ID = re.compile(r"(\d{4}\.\d{4,5})(v[1-9]\d*)?")
APP = "arXiv Assembly Watch"


def key_for(*parts):
    digest = hashlib.sha256(("\n".join((APP, *parts))).encode()).digest()
    alphabet = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
    return "".join(alphabet[n % len(alphabet)] for n in digest[:8])


def dpapi(data, encrypt):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    srcbuf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(srcbuf, ctypes.POINTER(ctypes.c_ubyte)))
    dst = Blob()
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    function = dll.CryptProtectData if encrypt else dll.CryptUnprotectData
    if not function(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(dst.data)


class APIError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"Zotero HTTP {status}: {message}")


def open_zotero():
    candidates = [shutil.which("zotero.exe")]
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if folder := os.environ.get(variable):
            candidates.extend([Path(folder) / "Zotero/zotero.exe", Path(folder) / "Programs/Zotero/zotero.exe"])
    executable = next((p for p in candidates if p and Path(p).is_file()), None)
    if not executable:
        raise RuntimeError("Start Zotero, then rerun the import.")
    os.startfile(str(executable), show_cmd=2)


class Zotero:
    def __init__(self):
        self.server = ""
        self.key = ""
        try:
            try:
                _, headers = self.request("GET", "/")
            except ConnectionRefusedError:
                if os.name != "nt":
                    raise RuntimeError("Run this command with Windows Python to reach the local Zotero API.")
                open_zotero()
                for attempt in range(30):
                    time.sleep(1)
                    try:
                        _, headers = self.request("GET", "/")
                        break
                    except ConnectionRefusedError:
                        if attempt == 29:
                            raise RuntimeError("Zotero opened but its local API did not become reachable.")
        except APIError as e:
            if e.status == 403:
                raise RuntimeError("Enable Zotero Settings > Advanced > Allow other applications on this computer to communicate with Zotero.") from e
            raise
        self.server = headers.get("Zotero-Server-ID", "")
        if not self.server:
            raise RuntimeError("Zotero 10 or later is required.")

    def request(self, method, path, body=None, headers=None, auth=False):
        h = {"Zotero-API-Version": "3", **(headers or {})}
        if self.server:
            h["Zotero-Server-ID"] = self.server
        if auth:
            if not self.key:
                raise RuntimeError("Run zotero.py authorize on Windows, then retry.")
            h["Zotero-API-Key"] = self.key
        if isinstance(body, dict):
            h["Content-Type"] = "application/json"
            body = json.dumps(body, ensure_ascii=False).encode()
        conn = http.client.HTTPConnection("127.0.0.1", 23119, timeout=180)
        try:
            conn.request(method, "/api" + path, body, h)
            r = conn.getresponse()
            raw = r.read()
            if r.status >= 300:
                if r.status == 401:
                    raise APIError(401, "Authorization expired. Run zotero.py authorize.")
                raise APIError(r.status, raw.decode(errors="replace")[:320])
            if raw and "application/json" in r.headers.get("Content-Type", ""):
                return json.loads(raw), r.headers
            return raw, r.headers
        finally:
            conn.close()

    def obj(self, path):
        try:
            return self.request("GET", LIB + path)[0]["data"]
        except APIError as e:
            if e.status != 404:
                raise
            return None

    def create(self, path, values):
        self.request("PUT", LIB + path, dict(values, version=0), auth=True)

    def secret(self, write=False):
        if os.name != "nt":
            raise RuntimeError("Use Windows Python to reach Zotero localhost and Windows DPAPI.")
        folder = Path(os.environ["LOCALAPPDATA"]) / "arxiv-assembly-watch"
        cache = folder / (hashlib.sha256(self.server.encode()).hexdigest()[:16] + ".dpapi")
        if write:
            resp, _ = self.request("POST", "/local/authorize", {"appName": APP})
            if not resp.get("remember"):
                raise RuntimeError("Daily unattended writes require Always Allow. Run authorize again and choose it in Zotero.")
            secret = dpapi(resp["key"].encode("ascii"), True)
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as out:
                out.write(secret)
            os.replace(out.name, cache)
        else:
            if not cache.exists():
                raise RuntimeError("Run zotero.py authorize once and select Always Allow in Zotero.")
            self.key = dpapi(cache.read_bytes(), False).decode("ascii")


def collection(api):
    roots, _ = api.request("GET", LIB + "collections/top")
    matches = [row["key"] for row in roots if row["data"]["name"] == COLLECTION]
    if len(matches) > 1:
        raise RuntimeError("Multiple top-level collections have the expected name.")
    if matches:
        return matches[0]
    key = key_for("collection", COLLECTION)
    api.create("collections/" + key, {"name": COLLECTION, "parentCollection": False})
    return key


def has_id(data, base):
    values = [data.get(field, "") for field in ("archiveID", "url", "DOI", "extra")]
    return any(re.search(r"(?<![\w.])" + re.escape(base) + r"(?:v\d+)?(?![\w.])", s, re.I)
               for s in values)


def parent(api, paper, base, col):
    key = key_for("parent", base)
    obj = api.obj("items/" + key)
    if obj and not has_id(obj, base):
        raise RuntimeError("Item key collision; existing item preserved.")
    if not obj:
        query = urlencode({"q": base, "qmode": "everything"})
        rows, _ = api.request("GET", LIB + "items/top?" + query)
        matches = [r["data"] for r in rows if has_id(r["data"], base)]
        if len(matches) > 1:
            raise RuntimeError(f"Multiple existing records match arXiv {base}; merge them in Zotero.")
        obj = matches[0] if matches else None
    if obj:
        key = obj["key"]
        cols = obj.get("collections", [])
        if col not in cols:
            api.request("PATCH", LIB + "items/" + key,
                        {"version": obj["version"], "collections": [*cols, col]}, auth=True)
        return key
    creators = []
    for name in paper["authors"]:
        first, _, last = name.strip().rpartition(" ")
        creators.append({"creatorType": "author", "firstName": first, "lastName": last})
    api.create("items/" + key, {"itemType": "preprint", "title": paper["title"],
               "creators": creators, "date": paper["published"], "abstractNote": paper["summary"],
               "repository": "arXiv", "archiveID": base, "url": "https://arxiv.org/abs/" + base,
               "collections": [col], "extra": "arXiv: " + base})
    return key


def child(api, parent_key, version, kind, data):
    key = key_for(parent_key, version, kind)
    path = "items/" + key
    obj = api.obj(path)
    if obj is None:
        api.create(path, dict(data, parentItem=parent_key))
        obj = api.obj(path)
    if not obj or obj.get("parentItem") != parent_key or obj.get("itemType") != data["itemType"]:
        raise RuntimeError(f"Failed to verify {kind} under the expected parent.")
    return obj


def attach(api, parent_key, paper_id, kind, path, blob):
    hashed = hashlib.md5(blob).hexdigest()
    filename = paper_id + ("-zh" if kind == "zh" else "") + ".pdf"
    title = ("PDF2zh 中文译本" if kind == "zh" else "arXiv 原文") + " · " + paper_id
    att = child(api, parent_key, paper_id, kind, {"itemType": "attachment", "linkMode": "imported_file",
                "title": title, "filename": filename, "contentType": "application/pdf"})
    item = "items/" + att["key"]
    if att.get("md5") == hashed:
        return att["key"]
    if att.get("md5") or att.get("filename") != filename:
        raise RuntimeError(f"Existing {kind} attachment differs; original data preserved.")
    pre = {"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}
    form = urlencode({"md5": hashed, "filename": filename, "filesize": len(blob),
                      "mtime": path.stat().st_mtime_ns // 1_000_000}).encode()
    staging, _ = api.request("POST", LIB + item + "/file", form, pre, auth=True)
    if not staging.get("exists"):
        token = staging["uploadKey"]
        if not re.fullmatch("[A-Za-z0-9]+", token):
            raise RuntimeError("Invalid Zotero upload token.")
        api.request("POST", "/local/uploads/" + token, blob,
                    {"Content-Type": "application/octet-stream"})
        api.request("POST", LIB + item + "/file", urlencode({"upload": token}).encode(), pre, auth=True)
    stored = api.obj(item)
    if not stored or stored.get("md5") != hashed:
        raise RuntimeError(f"Could not verify stored {kind} PDF hash.")
    return att["key"]


def read_pdf(path):
    blob = path.read_bytes()
    if b"%PDF-" not in blob[:1024] or b"%%EOF" not in blob[-4096:]:
        raise ValueError(f"Invalid or incomplete PDF: {path.name}")
    return blob


def import_paper(api, paper, pdf, zh):
    for field in ("id", "title", "published", "summary", "note"):
        if not isinstance(paper.get(field), str) or not paper[field].strip():
            raise ValueError(f"Missing paper field: {field}")
    m = ID.fullmatch(paper["id"])
    if not m or not isinstance(paper.get("authors"), list) or not paper["authors"] or not all(
            isinstance(a, str) and a.strip() for a in paper["authors"]):
        raise ValueError("Invalid arXiv ID or author list.")
    blobs = [("original", pdf, read_pdf(pdf))]
    if zh:
        blobs.append(("zh", zh, read_pdf(zh)))
        if blobs[0][2] == blobs[1][2]:
            raise ValueError("Original and translated files are identical.")
    col = collection(api)
    key = parent(api, paper, m[1], col)
    note_html = "<p><strong>arXiv 每日精选 · " + paper["id"] + "</strong></p>" + "".join(
        "<p>" + html.escape(line) + "</p>" for line in paper["note"].splitlines() if line.strip())
    note = child(api, key, paper["id"], "note", {"itemType": "note", "note": note_html})
    result = {"id": paper["id"], "collection": col, "parent": key, "note": note["key"]}
    for kind, path, blob in blobs:
        result[kind] = attach(api, key, paper["id"], kind, path, blob)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("authorize")
    cmd = sub.add_parser("import")
    cmd.add_argument("paper", type=Path)
    cmd.add_argument("pdf", type=Path)
    cmd.add_argument("--zh", type=Path)
    args = parser.parse_args()
    api = Zotero()
    if args.action == "authorize":
        api.secret(write=True)
        print("Zotero authorization saved for this Windows account.")
    else:
        api.secret()
        result = import_paper(api, json.loads(args.paper.read_text(encoding="utf-8-sig")), args.pdf, args.zh)
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        sys.exit(str(error))
