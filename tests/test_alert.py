"""Run with python3 -B tests/test_alert.py; all SMTP calls are mocked."""
from pathlib import Path
import smtplib
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import alert


class AlertTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=".")
        self.addCleanup(directory.cleanup)
        self.db = alert.connect_queue(Path(directory.name))
        self.addCleanup(self.db.close)
        self.to = alert.address("research@example.org")
        self.paper = alert.paper_id("2501.12345v2")
        self.server = Mock()

    def queue(self):
        return alert.enqueue(self.db, self.to, self.paper, "装配实验", "在实体机器人上的定位对照。")

    def test_missing_credentials_then_retry(self):
        self.assertEqual("pending", self.queue())
        with patch.object(alert, "open_mail", side_effect=ValueError("unconfigured")), self.assertRaises(ValueError):
            alert.flush(self.db, self.to)
        self.assertEqual("pending", self.queue())
        self.assertEqual(self.paper, alert.paper_id("2501.12345v3"))
        with patch.object(alert, "open_mail", return_value=("sender@example.org", self.server)):
            alert.flush(self.db, self.to)
            alert.flush(self.db, self.to)
        paper, problem = [c.args[0] for c in self.server.send_message.call_args_list]
        self.assertIn("邮件投递", problem["Subject"])
        self.assertNotIn("arxiv.org/abs", problem.get_content())
        msg = paper
        self.assertEqual(self.to, msg["To"])
        self.assertIn("https://arxiv.org/abs/2501.12345", msg.get_content())
        self.assertEqual("sent", self.queue())

    def test_response_lost_after_submit(self):
        self.queue()
        self.server.send_message.side_effect = smtplib.SMTPServerDisconnected()
        with patch.object(alert, "open_mail", return_value=("sender@example.org", self.server)):
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                alert.flush(self.db, self.to)
            self.server.send_message.side_effect = None
            alert.flush(self.db, self.to)
        self.assertEqual("uncertain", self.queue())
        subjects = [c.args[0]["Subject"] for c in self.server.send_message.call_args_list]
        self.assertEqual(1, sum("精选" in subject for subject in subjects))
        self.assertEqual(1, sum("跟踪异常" in subject for subject in subjects))

    def test_refusal_can_retry(self):
        self.queue()
        self.server.send_message.side_effect = smtplib.SMTPDataError(450, b"try later")
        with patch.object(alert, "open_mail", return_value=("sender@example.org", self.server)):
            with self.assertRaises(smtplib.SMTPDataError):
                alert.flush(self.db, self.to)
        self.assertEqual("pending", self.queue())

    def test_problem_dedup_recovery_and_recurrence(self):
        def problem():
            return alert.enqueue(self.db, self.to, "zotero", "无法连接 Zotero", "请打开桌面端", issue=True)
        self.assertEqual("pending", problem())
        self.assertEqual("pending", problem())
        self.assertEqual(1, alert.close_issue(self.db, self.to, "zotero"))
        self.assertEqual(0, alert.close_issue(self.db, self.to, "zotero"))
        self.assertEqual("pending", problem())
        with patch.object(alert, "open_mail", return_value=("sender@example.org", self.server)):
            alert.flush(self.db, self.to)
        self.assertEqual("sent", problem())
        messages = [c.args[0] for c in self.server.send_message.call_args_list]
        self.assertEqual(2, len(messages))
        first, second = messages
        self.assertNotEqual(first["Message-ID"], second["Message-ID"])
        self.assertIn("已恢复", first.get_content())
        self.assertNotIn("已恢复", second.get_content())
        self.assertNotIn("arxiv.org/abs", second.get_content())

    def test_old_queue_keeps_delivery_history(self):
        with TemporaryDirectory(dir=".") as folder:
            path = Path(folder)
            old = sqlite3.connect(path / "alerts.sqlite3")
            old.execute("""CREATE TABLE alerts (recipient TEXT, paper TEXT, title TEXT NOT NULL,
                        body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(recipient,paper))""")
            old.execute("INSERT INTO alerts VALUES(?, ?, 'paper', 'body', 'sent')", (self.to, self.paper))
            old.commit()
            old.close()
            updated = alert.connect_queue(path)
            try:
                self.assertEqual("sent", alert.enqueue(updated, self.to, self.paper, "review", "evidence"))
                self.assertEqual("pending", alert.enqueue(updated, self.to, "import", "导入失败", "请检查连接", issue=True))
            finally:
                updated.close()

    def test_reject_header_injection(self):
        for value in ["a@x.y,b@x.y", "a@x.y\r\nBcc: b@x.y", "A <a@x.y>", ".@x"]:
            with self.assertRaises(ValueError):
                alert.address(value)
        with self.assertRaises(ValueError):
            alert.enqueue(self.db, self.to, self.paper, "A\nBcc: b@x.y", "evidence")


if __name__ == "__main__":
    unittest.main()
