"""Configure this Windows user's QQ SMTP authorization code."""
import json
import os
from pathlib import Path
import tempfile

from zotero import dpapi


def cache_path():
    return Path(os.environ["LOCALAPPDATA"]) / "arxiv-assembly-watch" / "qq-smtp.dpapi"


def load():
    path = cache_path()
    if path.is_file():
        return json.loads(dpapi(path.read_bytes(), False))
    return None


def save(account, code):
    if not account.lower().endswith("@qq.com") or not code.strip():
        raise ValueError("请填写 QQ 邮箱的 SMTP 授权码。")
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = dpapi(json.dumps([account, code.strip()]).encode(), True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        out.write(encrypted)
    os.replace(out.name, path)


if __name__ == "__main__":
    from tkinter import Tk, simpledialog
    profile = Path(__file__).resolve().parents[1] / "profile.json"
    account = json.loads(profile.read_text(encoding="utf-8"))["alert_to"]
    app = Tk()
    app.withdraw()
    app.attributes("-topmost", True)
    value = simpledialog.askstring("QQ 邮箱发信设置", f"发件人：{account}\n请输入 QQ 邮箱 SMTP 授权码，将由 Windows 加密保存。", show="*", parent=app)
    if value:
        save(account, value)
    app.destroy()
