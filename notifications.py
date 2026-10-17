"""Offline notification evidence index. Never substitutes for native exports."""
from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def text(self):
        return "".join(c.text() if isinstance(c, Node) else c for c in self.children)

    def visible_text(self):
        return " ".join(c.visible_text() if isinstance(c, Node) else c for c in self.children)

    def nodes(self, tag):
        for c in self.children:
            if isinstance(c, Node):
                if c.tag == tag:
                    yield c
                yield from c.nodes(tag)


class Tree(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.current)
        self.current.children.append(n)
        if tag not in {"img", "br", "hr", "meta", "link", "input", "wbr"}:
            self.current = n

    def handle_endtag(self, tag):
        n = self.current
        while n.parent:
            if n.tag == tag:
                self.current = n.parent
                return
            n = n.parent

    def handle_data(self, data):
        self.current.children.append(data)


def link_fields(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname not in {"www.overleaf.com", "overleaf.com"}:
        return None
    q = parse_qs(p.query)
    project = (q.get("p") or [None])[0]
    if not project:
        m = re.fullmatch(r"/project/([A-Za-z0-9_-]+)", p.path)
        project = m[1] if m else None
    if not project:
        return None
    return {"project_id": project, "doc_id": (q.get("doc") or [None])[0],
            "change_id": (q.get("change") or [None])[0], "url": url}


def _section(text):
    parts = re.split(r"\s*[›>]\s*", text.strip())
    return parts[0], parts[1:]


def _html_records(html):
    root = Tree(html).root
    records, seen_cards = [], set()
    for p in root.nodes("p"):
        if list(p.nodes("table")) or list(p.nodes("p")):
            continue
        if not re.match(r"\s*[^›>\n]+\.(?:tex|bib|sty|cls)(?:\s*[›>]|\s*$)", p.text()):
            continue
        card = p.parent
        # The smallest enclosing cell owns the path, excerpt and optional link.
        while card.parent and card.tag != "td":
            card = card.parent
        if id(card) in seen_cards:
            continue
        seen_cards.add(id(card))
        links = [link_fields(a.attrs.get("href", "")) for a in card.nodes("a")]
        change = next((l for l in links if l and l["change_id"]), None)
        rejected = "Rejected your suggestion" in card.text()
        accepted = "Accepted your suggestion" in card.text()
        activity = "rejected" if rejected else "accepted" if accepted else "new_suggestion"
        file, sections = _section(p.text())
        rows = []
        for tr in card.nodes("tr"):
            cells = [n for n in tr.children if isinstance(n, Node) and n.tag == "td"]
            if len(cells) == 2 and cells[0].text().strip().isdigit():
                rows.append({"line": int(cells[0].text().strip()),
                             "excerpt": cells[1].text(),
                             "insertions": [n.text() for n in cells[1].nodes("ins")],
                             "deletions": [n.text() for n in cells[1].nodes("del")]})
        authors = [s.text().strip() for s in card.nodes("span")
                   if "font-weight: 600" in s.attrs.get("style", "")]
        records.append({**(change or {}), "file": file, "sections": sections,
                        "author": authors[0] if authors else None, "activity": activity,
                        "lines": rows, "excerpt": "\n".join(r["excerpt"] for r in rows),
                        "evidence": "notification_excerpt", "native_status": "unknown"})
    return root, records


def _plain_records(text):
    records = []
    pattern = r"^\[([^\n]+)\](?: by ([^\n]+))?\n(.*?)(?=^\[|\Z)"
    for m in re.finditer(pattern, text, re.M | re.S):
        body = m[3]
        u = re.search(r"View suggestion:\s*(https://[^\s]+)", body)
        change = link_fields(u[1]) if u else None
        activity = ("rejected" if "Rejected your suggestion" in body else
                    "accepted" if "Accepted your suggestion" in body else "new_suggestion")
        if not change and activity == "new_suggestion":
            continue
        file, sections = _section(m[1])
        # Preserve the diff excerpt, without inventing HTML-only line numbers.
        excerpt = body.split("View suggestion:")[0].strip() if u else body.split("\nRegards,")[0].strip()
        records.append({**(change or {}), "file": file, "sections": sections,
                        "author": m[2], "activity": activity, "lines": [], "excerpt": excerpt,
                        "evidence": "notification_excerpt", "native_status": "unknown"})
    return records


def parse_notification(message):
    """Parse normalized bodies; missing or omitted content always remains unknown."""
    html, plain = message.get("html", ""), message.get("text", "").replace("\r\n", "\n")
    root, records = _html_records(html) if html else (None, _plain_records(plain))
    text = root.visible_text() if root else plain
    urls = [a.attrs.get("href", "") for a in root.nodes("a")] if root else re.findall(r"https://[^\s)]+", plain)
    projects = {f["project_id"] for u in urls if (f := link_fields(u))}
    if len(projects) != 1:
        raise ValueError("Notification must identify exactly one Overleaf project")
    pid = projects.pop()
    if any(r.get("project_id", pid) != pid for r in records):
        raise ValueError("Conflicting project IDs")
    for r in records:
        r["project_id"] = pid
    total = re.search(r"\b(\d+) new suggestions?\b", text, re.I)
    reported = int(total[1]) if total else (1 if re.search(r"\bNew suggestion\b", text, re.I) else None)
    omitted = re.search(r"\b(\d+) more suggestions?\b", text, re.I)
    omitted = int(omitted[1]) if omitted else None
    visible = len({(r.get("doc_id"), r.get("change_id")) for r in records
                   if r["activity"] == "new_suggestion" and r.get("change_id")})
    warnings = []
    if reported is not None and reported != visible + (omitted or 0):
        warnings.append("Reported count does not match parsed and explicitly omitted suggestions")
    if html and not records and plain:
        warnings.append("HTML cards unrecognized; used plain text without HTML line positions")
        fallback = parse_notification({**message, "html": ""})
        fallback["warnings"].extend(warnings)
        return fallback
    return {"schema_version": "notification-v1", "project_id": pid,
            "source": {"gmail_id": message.get("gmail_id"), "message_id": message.get("message_id"),
                       "subject": message.get("subject"), "date": message.get("date")},
            "reported_new": reported, "visible_new": visible, "explicitly_omitted": omitted,
            "unexpanded_new": max(reported - visible, omitted or 0, 0) if reported is not None else omitted,
            "coverage": "partial_notification", "native_data_complete": False,
            "records": records, "warnings": warnings}


def _decode_body(body):
    if body.get("content") is not None:
        return body["content"]
    data = body.get("data") or body.get("base64_url_content") or ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8") if data else ""


def gmail_message(obj):
    """Accept Gmail API full JSON, connector structuredContent or normalized JSON."""
    obj = obj.get("structuredContent", obj)
    if "payload" not in obj:
        if "raw" in obj:
            raw = obj["raw"]
            return eml_message(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return obj
    payload = obj["payload"]
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    result = {"gmail_id": obj.get("id"), "message_id": headers.get("message-id"),
              "subject": headers.get("subject"), "date": headers.get("date")}
    def walk(part):
        mime = part.get("mimeType") or part.get("mime_type")
        if not part.get("filename") and mime in {"text/plain", "text/html"}:
            key = "html" if mime == "text/html" else "text"
            result[key] = result.get(key, "") + _decode_body(part.get("body") or {})
        for child in part.get("parts") or []:
            walk(child)
    walk(payload)
    return result


def eml_message(data):
    msg = BytesParser(policy=policy.default).parsebytes(data)
    out = {"message_id": msg.get("Message-ID"), "subject": str(msg.get("Subject", "")),
           "date": msg.get("Date")}
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() in {"text/plain", "text/html"}:
            key = "html" if part.get_content_type() == "text/html" else "text"
            out[key] = out.get(key, "") + part.get_content()
    return out


def load_notifications(path):
    if path.suffix.lower() == ".eml":
        return [parse_notification(eml_message(path.read_bytes()))]
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj = obj.get("structuredContent", obj) if isinstance(obj, dict) else obj
    messages = obj if isinstance(obj, list) else obj.get("messages", [obj])
    return [parse_notification(gmail_message(m)) for m in messages]


def ingest_notifications(notices, root):
    """Union evidence only. No absence/deletion inference; single writer required."""
    from bridge import _write_atomic
    location = root / "notifications"
    target = location / "index.json"
    index = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {
        "schema_version": "notification-index-v1", "notices": {}, "suggestions": {},
        "unlinked_activities": {}, "native_data_complete": False}
    events = []
    for notice in notices:
        source = notice["source"]
        message_key = source.get("message_id") or source.get("gmail_id")
        version = digest({k: v for k, v in notice.items() if k != "source"})
        key = digest([message_key, version])
        if key in index["notices"]:
            continue
        index["notices"][key] = notice
        events.append({"kind": "notification_observed", "notice": key})
        for i, record in enumerate(notice["records"]):
            cid = record.get("change_id")
            if cid:
                identity = json.dumps([record["project_id"], record.get("doc_id"), cid],
                                      separators=(",", ":"))
                if identity not in index["suggestions"]:
                    index["suggestions"][identity] = {"project_id": record["project_id"],
                        "doc_id": record.get("doc_id"), "change_id": cid, "observations": [],
                        "native_status": "unknown"}
                    events.append({"kind": "suggestion_discovered", "identity": identity})
                index["suggestions"][identity]["observations"].append({"notice": key, "record": i})
            else:
                index["unlinked_activities"][f"{key}:{i}"] = {"notice": key, "record": i,
                    "reason": "No change ID; cannot associate with a native change"}
    if events:
        # Immutable batch evidence first; index publication is atomic.
        batch = digest(notices)
        _write_atomic(location / "batches" / f"{batch}.json",
                      json.dumps({"notices": notices, "events": events}, ensure_ascii=False, indent=2) + "\n")
        _write_atomic(target, json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    return {"new_notices": sum(e["kind"] == "notification_observed" for e in events),
            "new_suggestions": sum(e["kind"] == "suggestion_discovered" for e in events),
            "events": events, "index": str(target)}
