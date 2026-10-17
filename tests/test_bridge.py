import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import (anchor_quality, cli, compare_payloads, export_file, get_rows,
                    import_payload, load_payload, project_id_from_url, summarize, verify_against_source)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "example_comments.json"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.payload = load_payload(EXAMPLE)

    def test_summary_and_anchor_types(self):
        summary = summarize(self.payload)
        self.assertEqual(summary["threads"], 3)
        self.assertEqual(summary["anchors"], 2)
        self.assertEqual(summary["matched_anchors"], 1)
        self.assertEqual(summary["point_only_anchors"], 1)
        self.assertEqual(summary["orphan_threads"], 1)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(anchor_quality({"stale": True, "anchored_text": "old"}), "stale")

    def test_rows_preserve_original_text_replies_and_orphans(self):
        rows = get_rows(self.payload)
        self.assertEqual(len(rows), 3)
        alpha = next(r for r in rows if r["thread_id"] == "thread-alpha")
        self.assertEqual(alpha["file"], "sections/introduction.tex")
        self.assertEqual(alpha["anchored_text"], "lightweight state summaries")
        self.assertEqual(alpha["reply_count"], 1)
        self.assertIn("latency measurements", alpha["replies_json"])
        orphan = next(r for r in rows if r["thread_id"] == "orphan-deleted-anchor")
        self.assertEqual(orphan["anchor_quality"], "unlocated")

    def test_reimport_is_idempotent_and_diff_is_meaningful(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = import_payload(self.payload, root)
            self.assertTrue(first["changed"])
            second = import_payload(copy.deepcopy(self.payload), root)
            self.assertFalse(second["changed"])
            self.assertEqual(second["events"], [])
            snapshots = list((root / "demo-paper-123" / "snapshots").glob("*.json"))
            self.assertEqual(len(snapshots), 1)
            next_payload = copy.deepcopy(self.payload)
            next_payload["threads"]["thread-alpha"]["messages"].append({
                "id": "msg-5", "role": "reply", "content": "Added a measured example.",
                "user": {"name": "Coauthor"}, "timestamp": "2026-10-17T11:00:00Z"})
            next_payload["threads"]["thread-beta"]["resolved"] = False
            next_payload["comments"][0]["line"] = 16
            third = import_payload(next_payload, root)
            kinds = {item["kind"] for item in third["events"]}
            self.assertEqual(kinds, {"new_message", "reopened", "anchor_changed"})
            self.assertEqual(len(list((root / "demo-paper-123" / "snapshots").glob("*.json"))), 2)

    def test_export_and_cli(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(cli(["ingest", "--file", str(EXAMPLE), "--store", str(root)]), 0)
            md = root / "comments.md"
            csv_file = root / "comments.csv"
            out = root / "comments_bridge.json"
            for kind, dest in [("md", md), ("csv", csv_file), ("json", out)]:
                export_file(self.payload, dest, kind)
                self.assertTrue(dest.is_file())
            self.assertIn("lightweight state summaries", md.read_text(encoding="utf-8"))
            self.assertIn("thread-alpha", csv_file.read_text(encoding="utf-8-sig"))
            self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))["rows"]), 3)
            self.assertEqual(cli(["export", "--store", str(root), "--out", str(root / "from_cli.md")]), 0)
            self.assertEqual(cli(["diff", "--store", str(root)]), 0)

    def test_verify_anchor_exact_and_changed_text(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "sections"
            source.mkdir()
            content = "x" * 450 + "lightweight state summaries" + " y"
            (source / "introduction.tex").write_text(content, encoding="utf-8")
            (source / "method.tex").write_text("\\begin{figure}", encoding="utf-8")
            result = verify_against_source(self.payload, Path(d))
            self.assertEqual(result[0]["result"], "exact")
            self.assertEqual(result[1]["result"], "point-only")
            (source / "introduction.tex").write_text(content.replace("lightweight", "large"), encoding="utf-8")
            self.assertEqual(verify_against_source(self.payload, Path(d))[0]["result"], "not-found")

    def test_project_url_validation(self):
        self.assertEqual(project_id_from_url("https://www.overleaf.com/project/abc123?x=1"), "abc123")
        with self.assertRaises(ValueError):
            project_id_from_url("https://unrelated.example/file")

    def test_jsonl_schema(self):
        with tempfile.TemporaryDirectory() as d:
            row = self.payload["comments"][0]
            line = {"type": "comment", "schema_version": "1.3", "project": self.payload["project"],
                    **row, "thread": self.payload["threads"][row["thread_id"]]}
            path = Path(d) / "comments.jsonl"
            path.write_text(json.dumps(line), encoding="utf-8")
            reconstructed = load_payload(path)
            self.assertEqual(reconstructed["project"]["id"], "demo-paper-123")
            self.assertEqual(len(reconstructed["comments"]), 1)
            self.assertIn("thread-alpha", reconstructed["threads"])


if __name__ == "__main__":
    unittest.main()