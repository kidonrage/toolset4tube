from __future__ import annotations

import argparse
import csv
import json
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "state.sqlite"
METADATA_DIR = DATA_DIR / "metadata"
SUBTITLES_DIR = DATA_DIR / "subtitles"
REPORTS_DIR = DATA_DIR / "reports"
AGENT_CONTEXT_PATH = REPORTS_DIR / "agent-context.md"
DECISIONS_PATH = REPORTS_DIR / "decisions.jsonl"
REPORT_PATH = REPORTS_DIR / "report.md"
DELETE_CSV_PATH = REPORTS_DIR / "delete-candidates.csv"

DECISION_LABELS = {
    "WATCH_FULLY",
    "WATCH_PARTIAL",
    "SUMMARY_ENOUGH",
    "DELETE",
    "LOW_CONFIDENCE",
}


class TerminalProgress:
    def __init__(self) -> None:
        self.enabled = sys.stderr.isatty()
        self.last_len = 0

    def update(self, message: str) -> None:
        if self.enabled:
            padding = " " * max(0, self.last_len - len(message))
            print(f"\r{message}{padding}", end="", file=sys.stderr, flush=True)
            self.last_len = len(message)
        else:
            print(message, file=sys.stderr, flush=True)

    def finish(self, message: str) -> None:
        if self.enabled and self.last_len:
            padding = " " * max(0, self.last_len - len(message))
            print(f"\r{message}{padding}", file=sys.stderr, flush=True)
        else:
            print(message, file=sys.stderr, flush=True)
        self.last_len = 0

    def end_live_line(self) -> None:
        if self.enabled and self.last_len:
            print(file=sys.stderr, flush=True)
            self.last_len = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_layout() -> None:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    SUBTITLES_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_layout()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            create table if not exists videos (
                video_id text primary key,
                url text not null,
                title text,
                channel text,
                duration integer,
                description text,
                upload_date text,
                playlist_position integer,
                raw_metadata_path text,
                scan_status text not null,
                last_error text,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists decisions (
                video_id text primary key,
                decision text not null,
                confidence real,
                basis text,
                reason text,
                watch_ranges_json text,
                summary text,
                created_at text not null,
                foreign key(video_id) references videos(video_id)
            )
            """
        )
        conn.commit()


def cmd_init(_: argparse.Namespace) -> int:
    init_db()
    print(f"Initialized {DB_PATH}")
    return 0


def run_ytdlp_entries(playlist_url: str, offset: int, limit: int, progress: TerminalProgress) -> tuple[list[dict[str, Any]], int]:
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp not found in PATH. Install yt-dlp separately, then rerun scan.")

    started_at = time.monotonic()
    entries: list[dict[str, Any]] = []
    bad_lines = 0
    current = 1 if limit else 0
    playlist_items = f"{offset + 1}:{offset + limit}"
    stopped_after_limit = False

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        proc = subprocess.Popen(
            ["yt-dlp", "--flat-playlist", "--dump-json", "--playlist-items", playlist_items, playlist_url],
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            bufsize=1,
        )
        try:
            while len(entries) + bad_lines < limit:
                if proc.stdout is None:
                    raise RuntimeError("yt-dlp stdout pipe was not created")

                ready, _, _ = select.select([proc.stdout], [], [], 0.5)
                if not ready:
                    if proc.poll() is not None:
                        break
                    elapsed = int(time.monotonic() - started_at)
                    progress.update(
                        progress_line(
                            "fetching metadata",
                            current,
                            limit,
                            len(entries),
                            bad_lines,
                            0,
                            f"elapsed: {elapsed}s",
                        )
                    )
                    continue

                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue

                current = min(len(entries) + bad_lines + 1, limit)
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    bad_lines += 1
                    progress.update(progress_line("fetching metadata", current, limit, len(entries), bad_lines, 0))
                    current = min(len(entries) + bad_lines + 1, limit)
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
                else:
                    bad_lines += 1
                progress.update(progress_line("fetching metadata", current, limit, len(entries), bad_lines, 0))
                current = min(len(entries) + bad_lines + 1, limit)
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise

        if proc.returncode is None:
            stopped_after_limit = len(entries) + bad_lines >= limit
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        stderr_file.seek(0)
        stderr = stderr_file.read()

    if proc.returncode != 0 and not stopped_after_limit:
        detail = stderr.strip() or "yt-dlp failed without output"
        raise RuntimeError(f"yt-dlp failed: {detail}")

    return entries, bad_lines


def value_as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def video_id_from_entry(entry: dict[str, Any]) -> str | None:
    value = entry.get("id") or entry.get("display_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    url = entry.get("url") or entry.get("webpage_url")
    if isinstance(url, str) and "watch?v=" in url:
        return url.rsplit("watch?v=", 1)[-1].split("&", 1)[0] or None
    return None


def video_url(video_id: str, entry: dict[str, Any]) -> str:
    for key in ("webpage_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return f"https://www.youtube.com/watch?v={video_id}"


def progress_line(
    stage: str,
    current: int,
    total: int,
    processed: int,
    errors: int,
    skipped: int,
    detail: str | None = None,
) -> str:
    total_percent = int((current / total) * 100) if total else 100
    line = (
        f"{stage} | video: {current}/{total} ({total_percent}%) | "
        f"processed: {processed}, errors: {errors}, skipped: {skipped}"
    )
    if detail:
        line = f"{line} | {detail}"
    return line


def mark_scan_error(conn: sqlite3.Connection, entry: dict[str, Any], index: int, error: Exception, now: str) -> None:
    video_id = video_id_from_entry(entry)
    if not video_id:
        return
    conn.execute(
        """
        insert into videos (
            video_id, url, title, channel, duration, description, upload_date,
            playlist_position, raw_metadata_path, scan_status, last_error,
            created_at, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(video_id) do update set
            scan_status = excluded.scan_status,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (
            video_id,
            video_url(video_id, entry),
            entry.get("title"),
            entry.get("channel") or entry.get("uploader") or entry.get("uploader_id"),
            value_as_int(entry.get("duration")),
            entry.get("description") or "",
            entry.get("upload_date") or entry.get("release_date"),
            value_as_int(entry.get("playlist_index")) or index,
            str(METADATA_DIR / f"{video_id}.json"),
            "ERROR",
            str(error),
            now,
            now,
        ),
    )


def cmd_scan(args: argparse.Namespace) -> int:
    init_db()
    progress = TerminalProgress()
    try:
        selected, fetch_errors = run_ytdlp_entries(args.playlist_url, args.offset, args.limit, progress)
    except RuntimeError as exc:
        progress.finish("fetching metadata failed | processed: 0, errors: 1")
        print(f"scan failed: {exc}", file=sys.stderr)
        return 2

    now = utc_now()
    scanned = 0
    skipped = 0
    errors = fetch_errors
    total = args.limit

    with connect_db() as conn:
        for index, entry in enumerate(selected, start=args.offset + 1):
            current = index - args.offset
            progress.update(progress_line("scanning metadata", current, total, scanned, errors, skipped))
            if not isinstance(entry, dict):
                skipped += 1
                progress.update(progress_line("scanning metadata", current, total, scanned, errors, skipped))
                continue
            video_id = video_id_from_entry(entry)
            if not video_id:
                skipped += 1
                progress.update(progress_line("scanning metadata", current, total, scanned, errors, skipped))
                continue

            try:
                raw_path = METADATA_DIR / f"{video_id}.json"
                raw_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

                position = value_as_int(entry.get("playlist_index")) or index
                conn.execute(
                    """
                    insert into videos (
                        video_id, url, title, channel, duration, description, upload_date,
                        playlist_position, raw_metadata_path, scan_status, last_error,
                        created_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(video_id) do update set
                        url = excluded.url,
                        title = excluded.title,
                        channel = excluded.channel,
                        duration = excluded.duration,
                        description = excluded.description,
                        upload_date = excluded.upload_date,
                        playlist_position = excluded.playlist_position,
                        raw_metadata_path = excluded.raw_metadata_path,
                        scan_status = excluded.scan_status,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        video_id,
                        video_url(video_id, entry),
                        entry.get("title"),
                        entry.get("channel") or entry.get("uploader") or entry.get("uploader_id"),
                        value_as_int(entry.get("duration")),
                        entry.get("description") or "",
                        entry.get("upload_date") or entry.get("release_date"),
                        position,
                        str(raw_path),
                        "METADATA_FETCHED",
                        None,
                        now,
                        now,
                    ),
                )
                scanned += 1
            except OSError as exc:
                errors += 1
                try:
                    mark_scan_error(conn, entry, index, exc, now)
                except sqlite3.Error:
                    pass
            except sqlite3.Error as exc:
                errors += 1
                try:
                    mark_scan_error(conn, entry, index, exc, now)
                except sqlite3.Error:
                    pass
            progress.update(progress_line("scanning metadata", current, total, scanned, errors, skipped))
        conn.commit()

    progress.end_live_line()
    print(f"Scanned: {scanned}")
    print(f"Errors: {errors}")
    print(f"Skipped: {skipped}")
    print(f"Metadata dir: {METADATA_DIR}")
    return 1 if errors else 0


def load_videos(limit: int) -> list[sqlite3.Row]:
    with connect_db() as conn:
        return list(
            conn.execute(
                """
                select
                    v.*,
                    d.decision as previous_decision,
                    d.confidence as previous_confidence,
                    d.reason as previous_reason
                from videos v
                left join decisions d on d.video_id = v.video_id
                order by coalesce(v.playlist_position, 999999), v.created_at, v.video_id
                limit ?
                """,
                (limit,),
            )
        )


def format_duration(seconds: Any) -> str:
    total = value_as_int(seconds)
    if total is None:
        return "unknown"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def excerpt(value: str | None, max_chars: int = 700) -> str:
    if not value:
        return ""
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def cmd_context(args: argparse.Namespace) -> int:
    init_db()
    profile_path = Path(args.profile)
    if not profile_path.exists():
        print(f"context failed: profile not found: {profile_path}", file=sys.stderr)
        return 2

    profile_text = profile_path.read_text()
    videos = load_videos(args.limit)
    lines: list[str] = [
        "# toolset4tube Agent Context",
        "",
        "This file is evidence for an AI agent. Do not mutate YouTube from this context.",
        "",
        "## Priority Profile",
        "",
        "```yaml",
        profile_text.rstrip(),
        "```",
        "",
        "## Decision Labels",
        "",
        "- WATCH_FULLY",
        "- WATCH_PARTIAL",
        "- SUMMARY_ENOUGH",
        "- DELETE",
        "- LOW_CONFIDENCE",
        "",
        "Write one JSON object per line to `data/reports/decisions.jsonl`.",
        "",
        "Required fields: `video_id`, `decision`, `confidence`, `basis`, `reason`, `watch_ranges`, `summary`, `created_at`.",
        "",
        "## Videos",
        "",
    ]

    if not videos:
        lines.extend(["No videos found. Run `toolset4tube scan PLAYLIST_URL --limit 50` first.", ""])
    for index, row in enumerate(videos, start=1):
        lines.extend(
            [
                f"### {index}. {row['title'] or '(untitled)'}",
                "",
                f"- video_id: `{row['video_id']}`",
                f"- url: {row['url']}",
                f"- channel: {row['channel'] or 'unknown'}",
                f"- duration: {format_duration(row['duration'])}",
                f"- published: {row['upload_date'] or 'unknown'}",
                f"- playlist_position: {row['playlist_position'] or 'unknown'}",
                f"- scan_status: {row['scan_status']}",
                f"- previous_decision: {row['previous_decision'] or 'none'}",
                f"- previous_confidence: {row['previous_confidence'] if row['previous_confidence'] is not None else 'none'}",
                "",
                "Description excerpt:",
                "",
                excerpt(row["description"]) or "(empty)",
                "",
            ]
        )

    AGENT_CONTEXT_PATH.write_text("\n".join(lines).rstrip() + "\n")
    print(f"Wrote {AGENT_CONTEXT_PATH}")
    print(f"Videos in context: {len(videos)}")
    return 0


def parse_decisions(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], [f"Missing decisions file: {path}"]

    decisions: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"Line {line_no}: invalid JSON: {exc}")
            continue
        if not isinstance(item, dict):
            errors.append(f"Line {line_no}: expected JSON object")
            continue
        video_id = item.get("video_id")
        decision = item.get("decision")
        if not isinstance(video_id, str) or not video_id.strip():
            errors.append(f"Line {line_no}: missing video_id")
            continue
        if decision not in DECISION_LABELS:
            errors.append(f"Line {line_no}: invalid decision for {video_id}: {decision}")
            continue
        decisions.append(item)
    return decisions, errors


def upsert_decisions(decisions: list[dict[str, Any]]) -> None:
    with connect_db() as conn:
        conn.execute("delete from decisions")
        for item in decisions:
            conn.execute(
                """
                insert into decisions (
                    video_id, decision, confidence, basis, reason,
                    watch_ranges_json, summary, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(video_id) do update set
                    decision = excluded.decision,
                    confidence = excluded.confidence,
                    basis = excluded.basis,
                    reason = excluded.reason,
                    watch_ranges_json = excluded.watch_ranges_json,
                    summary = excluded.summary,
                    created_at = excluded.created_at
                """,
                (
                    item["video_id"],
                    item["decision"],
                    item.get("confidence"),
                    item.get("basis"),
                    item.get("reason"),
                    json.dumps(item.get("watch_ranges") or [], ensure_ascii=False),
                    item.get("summary"),
                    item.get("created_at") or utc_now(),
                ),
            )
        conn.commit()


def report_rows() -> list[sqlite3.Row]:
    with connect_db() as conn:
        return list(
            conn.execute(
                """
                select
                    v.video_id, v.url, v.title, v.channel, v.duration, v.upload_date,
                    v.playlist_position,
                    d.decision, d.confidence, d.basis, d.reason,
                    d.watch_ranges_json, d.summary, d.created_at as decision_created_at
                from decisions d
                left join videos v on v.video_id = d.video_id
                order by
                    case d.decision
                        when 'WATCH_FULLY' then 1
                        when 'WATCH_PARTIAL' then 2
                        when 'SUMMARY_ENOUGH' then 3
                        when 'LOW_CONFIDENCE' then 4
                        when 'DELETE' then 5
                        else 9
                    end,
                    coalesce(v.playlist_position, 999999),
                    d.video_id
                """
            )
        )


def known_video_ids() -> set[str]:
    with connect_db() as conn:
        return {row["video_id"] for row in conn.execute("select video_id from videos")}


def write_delete_csv(rows: list[sqlite3.Row]) -> None:
    with DELETE_CSV_PATH.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["video_id", "title", "url", "confidence", "reason"])
        for row in rows:
            if row["decision"] == "DELETE":
                writer.writerow(
                    [
                        row["video_id"],
                        row["title"] or "",
                        row["url"] or "",
                        row["confidence"] if row["confidence"] is not None else "",
                        row["reason"] or "",
                    ]
                )


def cmd_report(_: argparse.Namespace) -> int:
    init_db()
    decisions, errors = parse_decisions(DECISIONS_PATH)
    known_ids = known_video_ids()
    for item in decisions:
        if item["video_id"] not in known_ids:
            errors.append(f"Unknown video_id in decisions: {item['video_id']}")

    valid_known = [item for item in decisions if item["video_id"] in known_ids]
    upsert_decisions(valid_known)
    rows = report_rows()
    write_delete_csv(rows)

    counts = {label: 0 for label in DECISION_LABELS}
    for row in rows:
        if row["decision"] in counts:
            counts[row["decision"]] += 1

    lines: list[str] = [
        "# toolset4tube Report",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Overview",
        "",
        f"- Total decisions: {len(rows)}",
        f"- Watch fully: {counts['WATCH_FULLY']}",
        f"- Watch partial: {counts['WATCH_PARTIAL']}",
        f"- Summary enough: {counts['SUMMARY_ENOUGH']}",
        f"- Delete: {counts['DELETE']}",
        f"- Low confidence: {counts['LOW_CONFIDENCE']}",
        "",
    ]

    for label in ["WATCH_FULLY", "WATCH_PARTIAL", "SUMMARY_ENOUGH", "DELETE", "LOW_CONFIDENCE"]:
        lines.extend([f"## {label}", ""])
        section_rows = [row for row in rows if row["decision"] == label]
        if not section_rows:
            lines.extend(["None.", ""])
            continue
        for row in section_rows:
            lines.extend(
                [
                    f"### {row['title'] or row['video_id']}",
                    "",
                    f"- video_id: `{row['video_id']}`",
                    f"- url: {row['url'] or 'unknown'}",
                    f"- channel: {row['channel'] or 'unknown'}",
                    f"- duration: {format_duration(row['duration'])}",
                    f"- confidence: {row['confidence'] if row['confidence'] is not None else 'unknown'}",
                    f"- basis: {row['basis'] or 'unknown'}",
                    "",
                    "Reason:",
                    "",
                    row["reason"] or "(empty)",
                    "",
                ]
            )
            if row["watch_ranges_json"] and row["watch_ranges_json"] != "[]":
                lines.extend(["Watch ranges:", "", f"```json\n{row['watch_ranges_json']}\n```", ""])
            if row["summary"]:
                lines.extend(["Summary:", "", row["summary"], ""])

    if errors:
        lines.extend(["## Errors", ""])
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    REPORT_PATH.write_text("\n".join(lines).rstrip() + "\n")
    print(f"Wrote {REPORT_PATH}")
    print(f"Wrote {DELETE_CSV_PATH}")
    if errors:
        print(f"Report completed with {len(errors)} error(s). See {REPORT_PATH}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolset4tube",
        description="Local YouTube playlist evidence collector for AI-assisted curation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize local data directories and SQLite schema.")
    init_parser.set_defaults(func=cmd_init)

    scan_parser = subparsers.add_parser("scan", help="Scan playlist metadata through yt-dlp.")
    scan_parser.add_argument("playlist_url")
    scan_parser.add_argument("--limit", type=int, default=50)
    scan_parser.add_argument("--offset", type=int, default=0)
    scan_parser.set_defaults(func=cmd_scan)

    context_parser = subparsers.add_parser("context", help="Build Markdown context for an AI agent.")
    context_parser.add_argument("--profile", default="profile.example.yaml")
    context_parser.add_argument("--limit", type=int, default=50)
    context_parser.set_defaults(func=cmd_context)

    report_parser = subparsers.add_parser("report", help="Render report from agent decisions.")
    report_parser.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "limit") and args.limit < 1:
        parser.error("--limit must be >= 1")
    if hasattr(args, "offset") and args.offset < 0:
        parser.error("--offset must be >= 0")
    return args.func(args)
