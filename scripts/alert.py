#!/usr/bin/env python3
"""Queue paper recommendations and actionable workflow problems for the recipient in profile.json."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import sys
from contextlib import closing
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate

PROFILE = Path(__file__).resolve().parents[1] / "profile.json"
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}|[a-zA-Z][\w.-]*/\d{7})(?:v[1-9]\d*)?", re.ASCII)


def paper_id(value):
    match = ARXIV_ID.fullmatch(value)
    if not match:
        raise ValueError("Supply the arXiv ID, e.g. 2501.12345v2")
    return match[1]


def issue_id(value):
    if not re.fullmatch("[a-z0-9][a-z0-9.-]{0,79}", value):
        raise ValueError("Use a stable issue ID of lowercase letters, digits, dots or dashes")
    return value


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Za-z0-9.-]+", value):
        raise ValueError("Expected one ASCII email address")
    try:
        parsed = Address(addr_spec=value)
    except (HeaderParseError, ValueError):
        raise ValueError("Invalid email address") from None
    if not parsed.username or not parsed.domain or parsed.addr_spec != value:
        raise ValueError("Expected one complete email address")
    return value


def connect_queue(directory):
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "alerts.sqlite3", timeout=20)
    db.execute("""CREATE TABLE IF NOT EXISTS alerts (
        recipient TEXT, paper TEXT, title TEXT NOT NULL, body TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY (recipient, paper))""")
    db.execute("""CREATE TABLE IF NOT EXISTS issues (
        recipient TEXT, name TEXT, episode INTEGER NOT NULL, active INTEGER NOT NULL,
        PRIMARY KEY (recipient, name))""")
    return db


def enqueue(db, recipient, paper, title, body, issue=False):
    if not title.strip() or any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise ValueError("Provide a single-line title")
    if not body.strip():
        raise ValueError("Provide a description")
    with db:
        if issue:
            name = issue_id(paper)
            db.execute("""INSERT INTO issues VALUES(?, ?, 1, 1) ON CONFLICT(recipient, name) DO UPDATE
                SET episode=episode+1, active=1 WHERE active=0""", (recipient, name))
            episode = db.execute("SELECT episode FROM issues WHERE recipient=? AND name=?",
                                 (recipient, name)).fetchone()[0]
            paper = f"issue:{name}:{episode}"
            body = f"发现时间：{formatdate(localtime=True)}\n\n{body}"
        db.execute("""INSERT INTO alerts(recipient, paper, title, body) VALUES(?, ?, ?, ?)
            ON CONFLICT(recipient, paper) DO NOTHING""", (recipient, paper, title.strip(), body.strip()))
    return db.execute("SELECT state FROM alerts WHERE recipient=? AND paper=?", (recipient, paper)).fetchone()[0]


def open_mail():
    username = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    saved_qq = False
    if not username and not password and os.name == "nt" and os.getenv("LOCALAPPDATA"):
        import qq_mail
        credentials = qq_mail.load()
        if credentials:
            username, password = credentials
            saved_qq = True
    if not username or not password:
        raise ValueError("Alerts queued. Set SMTP_USER and SMTP_PASSWORD locally for delivery.")
    sender = address(username)
    host = "smtp.qq.com" if saved_qq else os.getenv("SMTP_HOST") or ("smtp.qq.com" if sender.lower().endswith("@qq.com") else "")
    mode = "ssl" if saved_qq else os.getenv("SMTP_TLS", "ssl").lower()
    if not host or mode not in {"ssl", "starttls"}:
        raise ValueError("Set SMTP_HOST and use SMTP_TLS=ssl or starttls")
    port = 465 if saved_qq else int(os.getenv("SMTP_PORT") or (465 if mode == "ssl" else 587))
    context = ssl.create_default_context()
    if mode == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        if mode == "starttls":
            server.starttls(context=context)
        server.login(username, password)
    except BaseException:
        server.close()
        raise
    return sender, server


def close_issue(db, recipient, name):
    with db:
        changed = db.execute("UPDATE issues SET active=0 WHERE recipient=? AND name=? AND active=1",
                             (recipient, issue_id(name))).rowcount
        if changed:
            episode = db.execute("SELECT episode FROM issues WHERE recipient=? AND name=?",
                                 (recipient, name)).fetchone()[0]
            db.execute("UPDATE alerts SET body=body || ? WHERE recipient=? AND paper=? AND state='pending'",
                       (f"\n\n已恢复：{formatdate(localtime=True)}", recipient, f"issue:{name}:{episode}"))
    return changed


def update(db, recipient, paper, before, after):
    with db:
        return db.execute("UPDATE alerts SET state=? WHERE recipient=? AND paper=? AND state=?",
                          (after, recipient, paper, before)).rowcount


def flush(db, recipient):
    try:
        if send_pending(db, recipient):
            close_issue(db, recipient, "smtp-delivery")
    except (ValueError, OSError, smtplib.SMTPException) as error:
        enqueue(db, recipient, "smtp-delivery", "邮件投递曾遇到问题",
                f"邮件服务返回 {type(error).__name__}。任务已记录待发通知。"
                "如仍有未投递邮件，请检查发信账户授权和网络，再重试 flush。", issue=True)
        raise


def send_pending(db, recipient):
    rows = db.execute("SELECT paper, title, body FROM alerts WHERE recipient=? AND state='pending' ORDER BY rowid",
                      (recipient,)).fetchall()
    if not rows:
        print("No pending alerts")
        return
    sender, server = open_mail()
    with closing(server):
        for paper, title, body in rows:
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = recipient
            problem = paper.startswith("issue:")
            msg["Subject"] = f"arXiv 跟踪异常 | {title}" if problem else f"arXiv 精选 | {title}"
            digest = hashlib.sha256(f"{recipient}\0{paper}".encode()).hexdigest()
            msg["Message-ID"] = f"<{digest}@arxiv-watch.local>"
            msg["Date"] = formatdate(localtime=True)
            content = f"{title}\n\n{body}"
            if not problem:
                content += f"\n\nhttps://arxiv.org/abs/{paper}"
            msg.set_content(content)
            # Commit before SMTP so a crash after remote acceptance cannot trigger a duplicate.
            if not update(db, recipient, paper, "pending", "uncertain"):
                continue
            try:
                server.send_message(msg, from_addr=sender, to_addrs=[recipient])
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
                update(db, recipient, paper, "uncertain", "pending")
                raise
            except (smtplib.SMTPException, OSError):
                print(f"{paper}: uncertain; verify mailbox before resolving", file=sys.stderr)
                raise
            update(db, recipient, paper, "uncertain", "sent")
            print(f"{paper}: accepted by SMTP server")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("enqueue", help="Queue a reviewed paper")
    add.add_argument("paper")
    add.add_argument("--title", required=True)
    add.add_argument("--body", required=True)
    issue = commands.add_parser("issue", help="Queue an actionable problem once per incident")
    issue.add_argument("issue_id", type=issue_id)
    issue.add_argument("--title", required=True)
    issue.add_argument("--body", required=True)
    recovered = commands.add_parser("issue-resolved", help="Mark a verified recovery so a later recurrence can alert")
    recovered.add_argument("issue_id", type=issue_id)
    commands.add_parser("flush", help="Send pending alerts")
    commands.add_parser("status", help="Print paper and incident IDs with their delivery states")
    resolve = commands.add_parser("resolve", help="After verifying delivery manually, resolve an uncertain alert")
    resolve.add_argument("paper")
    resolve.add_argument("--as", choices=["sent", "pending"], required=True, dest="state")
    args = parser.parse_args()
    recipient = address(json.loads(PROFILE.read_text(encoding="utf-8-sig"))["alert_to"])
    with closing(connect_queue(Path.cwd() / ".arxiv-watch")) as db:
        if args.command == "enqueue":
            paper = paper_id(args.paper)
            print(f"{paper}: {enqueue(db, recipient, paper, args.title, args.body)}")
        elif args.command == "issue":
            print(enqueue(db, recipient, args.issue_id, args.title, args.body, issue=True))
        elif args.command == "issue-resolved":
            print("Closed" if close_issue(db, recipient, args.issue_id) else "No open issue")
        elif args.command == "flush":
            flush(db, recipient)
        elif args.command == "resolve":
            key = args.paper if re.fullmatch(r"issue:[a-z0-9][a-z0-9.-]{0,79}:\d+", args.paper) else paper_id(args.paper)
            if not update(db, recipient, key, "uncertain", args.state):
                raise ValueError("No uncertain alert with that ID")
        else:
            for paper, state in db.execute("SELECT paper, state FROM alerts WHERE recipient=? ORDER BY rowid",
                                            (recipient,)):
                print(f"{paper}: {state}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, sqlite3.Error, smtplib.SMTPException) as error:
        # SMTP errors may echo private addresses or server-supplied text.
        print(type(error).__name__ if isinstance(error, smtplib.SMTPException) else str(error), file=sys.stderr)
        sys.exit(1)
