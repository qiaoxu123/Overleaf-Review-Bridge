import base64
import copy
from email.message import EmailMessage
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import cli, compare_payloads, export_file, import_payload, load_payload
from notifications import (gmail_message, ingest_notifications, load_notifications,
                           parse_notification)

FIXTURE = Path(__file__).parent / "fixtures" / "notification_20261017_redacted.json"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "example_comments.json"


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.message = json.loads(FIXTURE.read_text())
        self.notice = parse_notification(self.message)

    def test_real_template_multiple_omitted_and_rejection(self):
        n = self.notice
        self.assertEqual((n["reported_new"], n["visible_new"], n["explicitly_omitted"]), (12, 3, 9))
        self.assertEqual(n["unexpanded_new"], 9)
        self.assertFalse(n["native_data_complete"])
        self.assertEqual(n["warnings"], [])
        self.assertEqual(len(n["records"]), 4)
        self.assertEqual([r.get("change_id") for r in n["records"]], ["change-a", "change-b", "change-c", None])
        self.assertEqual(n["records"][3]["activity"], "rejected")
        self.assertEqual([x["line"] for x in n["records"][0]["lines"]], [1160, 1161])
        self.assertEqual(n["records"][0]["file"], "manuscript.tex")
        self.assertEqual(n["records"][0]["sections"], ["Method", "Main Steps"])
        self.assertTrue(n["records"][0]["lines"][1]["deletions"])
        self.assertTrue(n["records"][0]["lines"][1]["insertions"])
        self.assertEqual(n["records"][0]["author"], "Demo Author")
        self.assertIn("&change=change-a", n["records"][0]["url"])

    def test_repeat_import_and_cross_email_union(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(ingest_notifications([self.notice], root)["new_suggestions"], 3)
            before = (root / "notifications/index.json").read_bytes()
            self.assertEqual(ingest_notifications([self.notice], root)["events"], [])
            self.assertEqual(before, (root / "notifications/index.json").read_bytes())
            other = copy.deepcopy(self.notice)
            other["source"]["message_id"] = "<another@overleaf.com>"
            other["records"] = [other["records"][0]]
            other["records"][0]["change_id"] = "change-d"
            result = ingest_notifications([other], root)
            self.assertEqual(result["new_suggestions"], 1)
            index = json.loads((root / "notifications/index.json").read_text())
            self.assertEqual(len(index["suggestions"]), 4)
            self.assertEqual(len(index["unlinked_activities"]), 1)
            self.assertTrue(all(x["native_status"] == "unknown" for x in index["suggestions"].values()))
            self.assertFalse((root / "project-demo/latest.json").exists())

    def test_cross_email_duplicate_has_two_observations(self):
        other = copy.deepcopy(self.notice)
        other["source"]["message_id"] = "<repeat@overleaf.com>"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = ingest_notifications([self.notice, other], root)
            self.assertEqual(result["new_suggestions"], 3)
            index = json.loads((root / "notifications/index.json").read_text())
            self.assertTrue(all(len(v["observations"]) == 2 for v in index["suggestions"].values()))

    def test_same_message_dedup_across_transports(self):
        other = copy.deepcopy(self.notice)
        other["source"]["gmail_id"] = "gmail-extra-provenance"
        other["source"]["date"] = "2026-10-17T10:55:22Z"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(ingest_notifications([self.notice, other], root)["new_notices"], 1)

    def test_singular_omission_and_html_fallback(self):
        msg = copy.deepcopy(self.message)
        msg["html"] = msg["html"].replace("12 new suggestions", "4 new suggestions").replace(
            "9 more suggestions", "1 more suggestion")
        n = parse_notification(msg)
        self.assertEqual(n["explicitly_omitted"], 1)
        self.assertEqual(n["warnings"], [])
        msg["text"] = ("New suggestion\n[main.tex > Method] by Reviewer\nexcerpt\n"
            "View suggestion: https://www.overleaf.com/notifications/open?p=project-demo&doc=d&change=c\n")
        msg["html"] = '<a href="https://www.overleaf.com/project/project-demo">Unknown template</a>'
        fallback = parse_notification(msg)
        self.assertEqual(fallback["visible_new"], 1)
        self.assertTrue(fallback["warnings"])

    def test_raw_gmail_and_evidence_revision(self):
        msg = EmailMessage()
        msg["Message-ID"] = "<raw@overleaf.com>"
        msg.set_content("New suggestion\n[main.tex > Method] by Author\nexcerpt\n"
            "View suggestion: https://www.overleaf.com/notifications/open?p=project-demo&doc=d&change=c")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
        self.assertEqual(parse_notification(gmail_message({"raw": raw}))["visible_new"], 1)
        enriched = copy.deepcopy(self.notice)
        enriched["records"][0]["excerpt"] += " additional evidence"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = ingest_notifications([self.notice, enriched], root)
            self.assertEqual(result["new_notices"], 2)
            self.assertEqual(result["new_suggestions"], 3)

    def test_project_doc_scoping_and_out_of_order(self):
        other = copy.deepcopy(self.notice)
        other["source"]["message_id"] = "<older@overleaf.com>"
        other["source"]["date"] = "Thu, 15 Oct 2026 10:00:00 +0000"
        other["project_id"] = "another-project"
        for r in other["records"]:
            r["project_id"] = "another-project"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(ingest_notifications([self.notice, other], root)["new_suggestions"], 6)
            same_project = copy.deepcopy(self.notice)
            same_project["records"][0]["doc_id"] = "other-doc"
            self.assertEqual(ingest_notifications([same_project], root)["new_suggestions"], 1)

    def test_plain_text_has_no_invented_line_numbers(self):
        text = ("Demo (https://www.overleaf.com/notifications/open?p=project-demo)\n"
                "New suggestion\n[main.tex > Method] by Author\n old\n + new\n"
                "View suggestion: https://www.overleaf.com/notifications/open?p=project-demo&doc=d&change=c\n")
        n = parse_notification({"text": text})
        self.assertEqual(n["reported_new"], 1)
        self.assertEqual(n["visible_new"], 1)
        self.assertEqual(n["records"][0]["lines"], [])
        self.assertIn("+ new", n["records"][0]["excerpt"])

    def test_truncated_body_and_unknown_counts_are_not_complete(self):
        n = parse_notification({"text": "12 new suggestions\nhttps://www.overleaf.com/project/demo"})
        self.assertEqual(n["unexpanded_new"], 12)
        self.assertEqual(n["visible_new"], 0)
        self.assertTrue(n["warnings"])
        self.assertFalse(n["native_data_complete"])
        unknown = parse_notification({"text": "https://www.overleaf.com/project/demo"})
        self.assertIsNone(unknown["reported_new"])
        self.assertIsNone(unknown["unexpanded_new"])

    def test_untrusted_or_conflicting_project_links(self):
        for text in ["https://www.overleaf.com.evil.example/project/demo",
                     "https://www.overleaf.com/project/a https://www.overleaf.com/project/b"]:
            with self.assertRaises(ValueError):
                parse_notification({"text": text})

    def test_gmail_api_and_connector_mime(self):
        html = self.message["html"]
        obj = {"id": "gmail-id", "payload": {"mimeType": "multipart/alternative",
            "headers": [{"name": "Message-ID", "value": "<api@overleaf.com>"}],
            "parts": [{"mimeType": "text/html", "body": {
                "data": base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")}}]}}
        n = parse_notification(gmail_message(obj))
        self.assertEqual(n["visible_new"], 3)
        obj["payload"]["parts"] = [{"mime_type": "text/html", "body": {"content": html}}]
        self.assertEqual(parse_notification(gmail_message({"structuredContent": obj}))["visible_new"], 3)

    def test_eml_and_cli_batch_json(self):
        msg = EmailMessage()
        msg["Subject"] = self.message["subject"]
        msg["Message-ID"] = self.message["message_id"]
        msg.set_content("Overleaf")
        msg.add_alternative(self.message["html"], subtype="html")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "mail.eml"
            path.write_bytes(msg.as_bytes())
            self.assertEqual(load_notifications(path)[0]["visible_new"], 3)
            self.assertEqual(cli(["ingest-mail", "--file", str(path), "--store", d]), 0)
            batch = root / "batch.json"
            batch.write_text(json.dumps([self.message, self.message]))
            self.assertEqual(len(load_notifications(batch)), 2)
            self.assertEqual(cli(["ingest-mail", "--file", str(batch), "--store", d]), 0)
            self.assertEqual(cli(["ingest-mail", "--file", str(root / "missing"), "--store", d]), 1)

    def test_mail_never_changes_native_archive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            payload = load_payload(EXAMPLE)
            payload["project"]["id"] = "project-demo"
            payload["tracked_changes"] = [{"id": "native-1", "content": "full native data"}]
            import_payload(payload, root)
            before = (root / "project-demo/latest.json").read_bytes()
            ingest_notifications([self.notice], root)
            self.assertEqual((root / "project-demo/latest.json").read_bytes(), before)
            self.assertEqual(json.loads(before)["tracked_changes"][0]["id"], "native-1")

    def test_native_suggestions_diff_and_separate_exports(self):
        payload = load_payload(EXAMPLE)
        payload["tracked_changes"] = [{"id": "c1", "doc_id": "d1", "insert": "new text"}]
        self.assertIn("new_tracked_change", {e["kind"] for e in compare_payloads(None, payload)})
        other = copy.deepcopy(payload)
        other["tracked_changes"][0]["insert"] = "updated"
        self.assertEqual([e["kind"] for e in compare_payloads(payload, other)], ["updated_tracked_change"])
        other["tracked_changes"] = []
        self.assertEqual(compare_payloads(payload, other)[0]["kind"], "missing_tracked_change")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for fmt in ["json", "md", "csv"]:
                path = root / ("suggestions." + fmt)
                export_file(payload, path, fmt, "suggestions")
                self.assertIn("new text", path.read_text(encoding="utf-8-sig"))
                self.assertNotIn("latency measurements", path.read_text(encoding="utf-8-sig"))
            out = json.loads((root / "suggestions.json").read_text())
            self.assertEqual(out["tracked_changes"], payload["tracked_changes"])


if __name__ == "__main__":
    unittest.main()
