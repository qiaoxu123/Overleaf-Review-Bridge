#!/usr/bin/env python3
"""Overleaf Review Bridge: read-only archive of Overleaf comment exports.

Live extraction is delegated to the MIT-licensed overleaf-comments-export CLI.
This module works offline on the JSON that CLI (or its browser extension) writes.
No passwords, cookies, browser profiles, or tokens are stored by this program.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

BRIDGE_VERSION = "0.2.0"
SCHEMA_VERSION = "bridge-v1"


def json_canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_component(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._")
    return name[:100] or "project"


def project_id_from_url(url: str) -> str:
    match = re.search(r"(?:^|/)project/([a-zA-Z0-9_-]+)(?:/|\?|#|$)", url)
    if not match:
        raise ValueError("需要完整的 Overleaf 项目链接，例如 https://www.overleaf.com/project/PROJECT_ID")
    return match.group(1)


def load_payload(path: Path) -> dict[str, Any]:
    if path.is_dir():
        path = path / "comments.json"
    if path.suffix == ".jsonl":
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        comments, threads = [], {}
        for record in records:
            if record.get("type") == "comment":
                comments.append({k: v for k, v in record.items() if k not in ("schema_version", "project", "type", "thread")})
            if record.get("thread"):
                thread = record["thread"]
                tid = thread.get("id") or record.get("thread_id")
                if tid:
                    threads[str(tid)] = thread
        if not records:
            raise ValueError("JSONL 文件为空")
        return {"project": records[0].get("project"), "threads": threads, "comments": comments,
                "schema_version": records[0].get("schema_version")}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("导出 JSON 顶层必须是对象")
    return data


def validate_payload(payload: dict[str, Any]) -> tuple[str, list[str]]:
    proj = payload.get("project")
    if not isinstance(proj, dict) or not proj.get("id"):
        raise ValueError("缺少 project.id：请提供原始 comments.json 或 comments.jsonl")
    if not isinstance(payload.get("comments"), list):
        raise ValueError("缺少 comments 数组")
    if not isinstance(payload.get("threads"), dict):
        raise ValueError("缺少 threads 对象")
    pid = str(proj["id"])
    warnings = []
    for i, c in enumerate(payload["comments"]):
        if not isinstance(c, dict) or not c.get("thread_id"):
            raise ValueError(f"comments[{i}] 缺少 thread_id")
        if str(c["thread_id"]) not in payload["threads"]:
            warnings.append(f"批注 {c['thread_id']} 未找到完整线程消息")
    return pid, warnings


def core_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Exclude fetch time/versions so repeat exports produce the same digest."""
    return {
        "project": payload["project"],
        "threads": payload["threads"],
        "comments": payload["comments"],
        "tracked_changes": payload.get("tracked_changes") or [],
        "orphan_thread_ids": payload.get("orphan_thread_ids") or [],
    }


def anchored_threads(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in payload.get("comments") or []:
        result.setdefault(str(item["thread_id"]), []).append(item)
    return result


def anchor_quality(c: dict[str, Any] | None) -> str:
    if not c:
        return "unlocated"
    if bool(c.get("stale")):
        return "stale"
    if not c.get("anchored_text"):
        return "point-only"
    return "matched"


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = anchored_threads(payload)
    threads = payload["threads"]
    return {
        "threads": len(threads),
        "tracked_changes": len(payload.get("tracked_changes") or []),
        "anchors": sum(map(len, mapping.values())),
        "resolved": sum(bool(t.get("resolved")) for t in threads.values() if isinstance(t, dict)),
        "orphan_threads": sum(tid not in mapping for tid in threads),
        "matched_anchors": sum(anchor_quality(c) == "matched" for c in payload["comments"]),
        "stale_anchors": sum(anchor_quality(c) == "stale" for c in payload["comments"]),
        "point_only_anchors": sum(anchor_quality(c) == "point-only" for c in payload["comments"]),
    }


def _thread_messages(payload: dict, tid: str) -> list[dict]:
    return (payload["threads"].get(tid) or {}).get("messages") or []


def _message_key(m: dict) -> str:
    return str(m.get("id") or hashlib.sha256(json_canonical(m).encode()).hexdigest()[:16])


def compare_payloads(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    if old is None:
        return ([{"kind": "new_thread", "thread_id": tid} for tid in sorted(new["threads"])]
                + [{"kind": "new_tracked_change", "change_key": k} for k in sorted(change_map(new))])
    changes: list[dict[str, Any]] = []
    old_threads, new_threads = old["threads"], new["threads"]
    aold, anew = anchored_threads(old), anchored_threads(new)
    for tid in sorted(set(new_threads) - set(old_threads)):
        changes.append({"kind": "new_thread", "thread_id": tid})
    for tid in sorted(set(old_threads) - set(new_threads)):
        changes.append({"kind": "missing_thread", "thread_id": tid, "note": "可能被删除或因筛选而缺失"})
    for tid in sorted(set(old_threads) & set(new_threads)):
        a, b = old_threads[tid], new_threads[tid]
        if bool(a.get("resolved")) != bool(b.get("resolved")):
            changes.append({"kind": "resolved" if b.get("resolved") else "reopened", "thread_id": tid})
        m_old = {_message_key(m): m for m in (a.get("messages") or [])}
        m_new = {_message_key(m): m for m in (b.get("messages") or [])}
        for mid in sorted(set(m_new) - set(m_old)):
            changes.append({"kind": "new_message", "thread_id": tid, "message_id": mid})
        for mid in sorted(set(m_new) & set(m_old)):
            if json_canonical(m_new[mid]) != json_canonical(m_old[mid]):
                changes.append({"kind": "edited_message", "thread_id": tid, "message_id": mid})
        # Compare anchor location by logical identity, not short IDs that shift on reordering.
        def sig(anchors: list[dict]) -> list[tuple]:
            return sorted((str(c.get("doc_id")), str(c.get("pathname")),
                           str(c.get("anchored_text")), int(c.get("offset") or 0),
                           str(c.get("line")), bool(c.get("stale"))) for c in anchors)
        if sig(aold.get(tid, [])) != sig(anew.get(tid, [])):
            changes.append({"kind": "anchor_changed", "thread_id": tid})
    before, after = change_map(old), change_map(new)
    for key in sorted(set(after) - set(before)):
        changes.append({"kind": "new_tracked_change", "change_key": key})
    for key in sorted(set(before) - set(after)):
        changes.append({"kind": "missing_tracked_change", "change_key": key,
                        "note": "Missing from this export; acceptance/deletion is unknown"})
    for key in sorted(set(before) & set(after)):
        if before[key] != after[key]:
            changes.append({"kind": "updated_tracked_change", "change_key": key})
    return changes


def change_map(payload):
    result = {}
    for change in payload.get("tracked_changes") or []:
        if isinstance(change, dict):
            cid = change.get("id") or change.get("change_id") or change.get("_id")
            key = json_canonical([change.get("doc_id"), str(cid)]) if cid else None
        else:
            key = None
        key = key or "content:" + hashlib.sha256(json_canonical(change).encode()).hexdigest()
        result[key] = change
    return result


def _write_atomic(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def store_dir(root: Path, pid: str) -> Path:
    return root / safe_component(pid)


def current_path(root: Path, pid: str) -> Path:
    return store_dir(root, pid) / "latest.json"


def import_payload(payload: dict[str, Any], root: Path) -> dict[str, Any]:
    pid, warnings = validate_payload(payload)
    root = root.resolve()
    location = store_dir(root, pid)
    location.mkdir(parents=True, exist_ok=True)
    current = current_path(root, pid)
    previous = json.loads(current.read_text(encoding="utf-8")) if current.exists() else None
    changes = compare_payloads(previous, payload)
    digest = hashlib.sha256(json_canonical(core_content(payload)).encode("utf-8")).hexdigest()[:16]
    date_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshots = location / "snapshots"
    snapshots.mkdir(exist_ok=True)
    # Snapshots identify unique semantic states; repeated polling doesn't create duplicates.
    snapshot = snapshots / f"{digest}.json"
    changed = previous is None or json_canonical(core_content(previous)) != json_canonical(core_content(payload))
    if not snapshot.exists():
        _write_atomic(snapshot, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _write_atomic(current, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    meta = {"bridge_schema_version": SCHEMA_VERSION, "project_id": pid, "sha256_short": digest,
            "imported_at": datetime.now(timezone.utc).isoformat(), "changed": changed,
            "summary": summarize(payload), "events": changes, "warnings": warnings,
            "snapshot": str(snapshot.relative_to(location))}
    _write_atomic(location / "last_import.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    if changed:
        _write_atomic(location / f"events_{date_stamp}_{digest}.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def locate_latest(root: Path, project: str | None) -> Path:
    if project:
        target = current_path(root, project)
        if not target.is_file():
            raise FileNotFoundError(f"尚未归档项目 {project}: {target}")
        return target
    matches = list(root.glob("*/latest.json"))
    if len(matches) != 1:
        raise ValueError("请使用 --project 指定项目 ID")
    return matches[0]


def verify_against_source(payload: dict[str, Any], source_root: Path) -> list[dict[str, Any]]:
    """Check Overleaf's anchored quote against saved .tex source (read-only).

    Avoid guessing a match when the same phrase occurs elsewhere in a file.
    """
    source_root = source_root.resolve()
    results: list[dict[str, Any]] = []
    for comment in payload.get("comments") or []:
        text = str(comment.get("anchored_text") or "")
        path = str(comment.get("pathname") or "")
        item = {"thread_id": str(comment["thread_id"]), "file": path, "line": comment.get("line")}
        if not text:
            item["result"] = "point-only"
            results.append(item)
            continue
        file = (source_root / path).resolve()
        if not file.is_relative_to(source_root):
            item["result"] = "invalid-path"
            results.append(item)
            continue
        if not file.is_file():
            item["result"] = "source-missing"
            results.append(item)
            continue
        body = file.read_text(encoding="utf-8")
        offset = int(comment.get("offset") or 0)
        if body[offset:offset + len(text)] == text:
            status = "exact"
        elif text in body[max(0, offset-200):offset+len(text)+200]:
            status = "nearby"
        elif text in body:
            status = "moved-far"
        else:
            status = "not-found"
        item["result"] = status
        results.append(item)
    return results


def get_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    comments_by_thread = anchored_threads(payload)
    rows = []
    for tid, t in sorted(payload["threads"].items()):
        messages = t.get("messages") or []
        anchors = comments_by_thread.get(tid) or [None]
        first = messages[0] if messages else {}
        author = (first.get("user") or {}).get("name") or (first.get("user") or {}).get("email") or ""
        for c in anchors:
            c = c or {}
            ctx = c.get("context") or {}
            rows.append({
                "project_id": str(payload["project"]["id"]), "thread_id": tid,
                "short_id": c.get("short_id") or "", "file": c.get("pathname") or "",
                "doc_id": c.get("doc_id") or "", "section": c.get("nearest_heading") or "",
                "line": c.get("line") or "", "column": c.get("col") if c.get("col") is not None else "",
                "offset": c.get("offset") if c.get("offset") is not None else "",
                "anchored_text": c.get("anchored_text") or "",
                "context_before": ctx.get("before") or "", "context_after": ctx.get("after") or "",
                "anchor_quality": anchor_quality(c if c else None),
                "status": "resolved" if t.get("resolved") else "open", "author": author,
                "created_at": first.get("timestamp") or "", "comment": first.get("content") or "",
                "reply_count": max(0, len(messages) - 1),
                "replies_json": json_canonical(messages[1:]),
            })
    return rows


def md_escape(s: Any) -> str:
    return str(s or "").replace("\r", "").replace("\n", " ").replace("|", "\\|")


def to_markdown(payload: dict[str, Any]) -> str:
    stats = summarize(payload)
    title = str(payload["project"].get("title") or payload["project"]["id"])
    rows = get_rows(payload)
    out = [f"# Overleaf 批注归档 — {title}", "",
           f"项目 ID：`{payload['project']['id']}`  ·  线程：{stats['threads']}  ·  "
           f"已解决：{stats['resolved']}  ·  精确锚点：{stats['matched_anchors']}  ·  "
           f"需复核：{stats['stale_anchors']}", "",
           "| 线程 ID | 文件:行 | 标注原文 | 批注 | 状态 |",
           "|---|---|---|---|---|"]
    for row in rows:
        quoted = row['anchored_text'] or "（仅定位点 / 无选中文本）"
        out.append("| " + " | ".join(md_escape(v) for v in [
            row['thread_id'], f"{row['file']}:{row['line']}", quoted,
            row['comment'][:200], row['status']]) + " |")
    out += ["", "## 线程详情", ""]
    thread_ids = set()
    for row in rows:
        tid = row["thread_id"]
        if tid in thread_ids:
            continue
        thread_ids.add(tid)
        th = payload["threads"][tid]
        out.extend([f"### {row['short_id'] or tid} — {row['file']}:{row['line']}", "",
                    f"- 原始线程 ID：`{tid}`；状态：{row['status']}；锚点：{row['anchor_quality']}",
                    f"- 所属章节：{row['section'] or '未知'}",
                    f"- 标注原文：`{row['anchored_text'] or '（位置批注）'}`",
                    f"- 上下文：{row['context_before']} **{row['anchored_text']}** {row['context_after']}", ""])
        for idx, msg in enumerate(th.get("messages") or []):
            who = (msg.get("user") or {}).get("name") or "未知作者"
            role = "批注" if idx == 0 else f"回复 {idx}"
            out.append(f"**{role} · {who} · {msg.get('timestamp') or '未知时间'}**：{msg.get('content') or ''}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def export_file(payload: dict[str, Any], output: Path, format_name: str, kind: str = "comments"):
    if kind == "suggestions":
        output.parent.mkdir(parents=True, exist_ok=True)
        changes = payload.get("tracked_changes") or []
        if format_name == "json":
            output.write_text(json.dumps({"schema_version": SCHEMA_VERSION,
                "project": payload["project"], "data_kind": "native_tracked_changes",
                "tracked_changes": changes}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif format_name == "csv":
            with output.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["project_id", "change_key", "raw_change_json"])
                writer.writerows([payload["project"]["id"], k, json_canonical(v)]
                                 for k, v in change_map(payload).items())
        elif format_name == "md":
            output.write_text("# Native tracked changes\n\n" + "\n\n".join(
                "```json\n" + json.dumps(c, ensure_ascii=False, indent=2) + "\n```" for c in changes) + "\n",
                encoding="utf-8")
        else:
            raise ValueError(f"Unsupported format: {format_name}")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "md":
        output.write_text(to_markdown(payload), encoding="utf-8")
    elif format_name == "csv":
        rows = get_rows(payload)
        fields = list(rows[0].keys()) if rows else ["project_id", "thread_id", "anchored_text", "comment"]
        with output.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    elif format_name == "json":
        obj = {"schema_version": SCHEMA_VERSION, "project": payload["project"], "summary": summarize(payload),
               "rows": get_rows(payload)}
        output.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        raise ValueError(f"Unsupported format: {format_name}")


def print_meta(meta: dict[str, Any]):
    s = meta["summary"]
    print(f"项目 {meta['project_id']} | 线程 {s['threads']} | 锚点 {s['anchors']} | "
          f"已解决 {s['resolved']} | 匹配 {s['matched_anchors']} | 过期 {s['stale_anchors']} | "
          f"无定位 {s['orphan_threads']}")
    print(f"归档 {'有变化' if meta['changed'] else '未变化'} | 新事件 {len(meta['events'])} | 快照 {meta['snapshot']}")
    for warning in meta.get("warnings") or []:
        print(f"警告: {warning}", file=sys.stderr)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Overleaf Review Bridge（只读，批注拉取与归档）")
    parser.add_argument("--version", action="version", version=BRIDGE_VERSION)
    subs = parser.add_subparsers(dest="command", required=True)
    pp = subs.add_parser("pull", help="调用 upstream exporter 拉取在线项目（需本机 Overleaf 登录）")
    pp.add_argument("--url", required=True, help="Overleaf 项目链接")
    pp.add_argument("--store", default="./review-store")
    pp.add_argument("--upstream-binary", default="overleaf-comments-export")
    pi = subs.add_parser("ingest", help="导入浏览器扩展/CLI 导出的 comments.json/jsonl")
    pi.add_argument("--file", required=True)
    pi.add_argument("--store", default="./review-store")
    pl = subs.add_parser("list", help="展示归档项目")
    pl.add_argument("--store", default="./review-store")
    pe = subs.add_parser("export", help="从归档导出 AI 使用的 JSON/CSV/Markdown")
    pe.add_argument("--store", default="./review-store")
    pe.add_argument("--project")
    pe.add_argument("--format", choices=["md", "csv", "json"], default="md")
    pe.add_argument("--out", required=True)
    pe.add_argument("--kind", choices=["comments", "suggestions"], default="comments")
    pn = subs.add_parser("ingest-mail", help="离线解析 EML / Gmail full JSON；单独保存通知证据索引")
    pn.add_argument("--file", required=True)
    pn.add_argument("--store", default="./review-store")
    pd = subs.add_parser("diff", help="展示最后一次导入的增量事件")
    pd.add_argument("--store", default="./review-store")
    pd.add_argument("--project")
    pv = subs.add_parser("verify", help="校对批注原文是否仍存在于导出的 .tex 源文件")
    pv.add_argument("--store", default="./review-store")
    pv.add_argument("--project")
    pv.add_argument("--source", help="上游导出目录中的 source/ 路径；默认读取 upstream-export/source")
    pdoc = subs.add_parser("doctor", help="检测环境/上游工具")
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest-mail":
            from notifications import load_notifications, ingest_notifications
            notices = load_notifications(Path(args.file))
            result = ingest_notifications(notices, Path(args.store))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "doctor":
            binary = shutil.which("overleaf-comments-export")
            print(f"Python: {sys.version.split()[0]}")
            print("上游导出工具: " + (binary or "未安装（pip install --upgrade overleaf-comments-export）"))
            print("Bridge 模式: read-only；无需输入 Overleaf 密码")
            return 0
        if args.command == "pull":
            pid = project_id_from_url(args.url)
            binary = shutil.which(args.upstream_binary)
            if not binary:
                raise RuntimeError('请先安装上游工具：python -m pip install --upgrade "overleaf-comments-export"')
            root = Path(args.store)
            out = store_dir(root, pid) / "upstream-export"
            out.mkdir(parents=True, exist_ok=True)
            # Credentials are never passed or persisted by this bridge.
            cmd = [binary, "--project-url", args.url, "--out", str(out), "--include-source", "--stable"]
            res = subprocess.run(cmd, check=False)
            if res.returncode:
                raise RuntimeError(f"上游工具返回错误码 {res.returncode}。请先用 --gui 检查登录状态。")
            meta = import_payload(load_payload(out / "comments.json"), root)
            print_meta(meta)
            return 0
        if args.command == "ingest":
            meta = import_payload(load_payload(Path(args.file)), Path(args.store))
            print_meta(meta)
            return 0
        if args.command == "list":
            root = Path(args.store)
            for f in sorted(root.glob("*/latest.json")):
                data = json.loads(f.read_text(encoding="utf-8"))
                print(f"{data['project']['id']}\t{data['project'].get('title') or ''}\t{summarize(data)['threads']} threads")
            return 0
        if args.command == "export":
            payload = load_payload(locate_latest(Path(args.store), args.project))
            path = Path(args.out)
            export_file(payload, path, args.format, args.kind)
            print(f"导出成功: {path}")
            return 0
        if args.command == "diff":
            f = locate_latest(Path(args.store), args.project).with_name("last_import.json")
            meta = json.loads(f.read_text(encoding="utf-8"))
            print(json.dumps(meta["events"], ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify":
            latest = locate_latest(Path(args.store), args.project)
            payload = load_payload(latest)
            source = Path(args.source) if args.source else latest.parent / "upstream-export" / "source"
            results = verify_against_source(payload, source)
            totals: dict[str, int] = {}
            for result in results:
                key = result["result"]
                totals[key] = totals.get(key, 0) + 1
            print(json.dumps({"counts": totals, "anchors": results}, ensure_ascii=False, indent=2))
            return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())