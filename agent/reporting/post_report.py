"""Send the nightly maintenance report to a Teams channel via Microsoft Graph API.

Reads /opt/qnoe-agent/logs/nightly_report.json written by nightly_run.py.
Uses the existing bot credentials (ChannelMessage.Send) to post to the
configured Teams channel.

Configuration (via /opt/qnoe-agent/secrets/report.env or environment):
  REPORT_TEAM_ID    — Teams team (group) ID
  REPORT_CHANNEL_ID — Teams channel ID
  (fallback: REPORT_CHAT_ID or REPORT_TO_EMAIL for DM delivery)

Bot credentials are read from /opt/qnoe-agent/secrets/sharepoint.env
(same account: SHAREPOINT_USERNAME / SHAREPOINT_PASSWORD).

Usage:
  python -m agent.reporting.post_report
  python -m agent.reporting.post_report --dry-run   # print without sending
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import msal
import requests

logger = logging.getLogger(__name__)

REPORT_JSON = Path(os.environ.get(
    "NIGHTLY_REPORT_JSON", "/opt/qnoe-agent/logs/nightly_report.json"
))
REPORT_TXT_DIR = Path(os.environ.get(
    "NIGHTLY_REPORT_TXT_DIR", "/opt/qnoe-agent/logs"
))
GRAPH = "https://graph.microsoft.com/v1.0"
APP_ID = "108a03c5-e265-4ab6-a5ea-9c902fd527d4"
TENANT_ID = "f78a768a-22ae-4432-9eb4-55ce4b73c8c3"

_SECRETS_FILES = [
    Path("/opt/qnoe-agent/secrets/report.env"),
    Path("/opt/qnoe-agent/secrets/sharepoint.env"),
]


def _load_secrets() -> dict:
    merged: dict[str, str] = {}
    for path in _SECRETS_FILES:
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    merged.setdefault(k.strip(), v.strip())
    merged.update(os.environ)
    return merged


def _authenticate(secrets: dict) -> str:
    username = secrets.get("SHAREPOINT_USERNAME") or secrets.get("TEAMS_USERNAME", "")
    password = secrets.get("SHAREPOINT_PASSWORD") or secrets.get("TEAMS_PASSWORD", "")
    if not username or not password:
        raise ValueError("No bot credentials found in secrets files")
    app = msal.PublicClientApplication(
        APP_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
    )
    result = app.acquire_token_by_username_password(
        username=username,
        password=password,
        scopes=["https://graph.microsoft.com/.default"],
    )
    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")
    return result["access_token"]


def _get_bot_upn(token: str) -> str:
    """Return the signed-in bot's UPN via /me."""
    h = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{GRAPH}/me?$select=userPrincipalName", headers=h, timeout=10)
    r.raise_for_status()
    return r.json()["userPrincipalName"]


def _get_or_create_chat(token: str, bot_upn: str, recipient_id: str) -> str:
    """Get or create a 1-on-1 chat between the bot and the recipient. Returns chat ID.

    recipient_id may be an object ID (GUID) or a UPN; object IDs are preferred because
    ROPC bots lack User.ReadBasic.All and cannot resolve UPNs via the Graph directory.
    """
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "chatType": "oneOnOne",
        "members": [
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"{GRAPH}/users/{bot_upn}",
            },
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"{GRAPH}/users/{recipient_id}",
            },
        ],
    }
    r = requests.post(f"{GRAPH}/chats", headers=h, json=body, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def _send_chat_message(token: str, chat_id: str, html_body: str) -> None:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"body": {"contentType": "html", "content": html_body}}
    r = requests.post(f"{GRAPH}/chats/{chat_id}/messages", headers=h, json=body, timeout=15)
    r.raise_for_status()


def _send_channel_message(token: str, team_id: str, channel_id: str, html_body: str) -> None:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"body": {"contentType": "html", "content": html_body}}
    url = f"{GRAPH}/teams/{team_id}/channels/{channel_id}/messages"
    r = requests.post(url, headers=h, json=body, timeout=15)
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_duration(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{int(s) // 60}m {int(s) % 60}s"
    return f"{s / 3600:.1f}h"


def _task_detail(t: dict) -> str:
    stats = t.get("stats") or {}
    n = t["name"]
    if n == "task_qdrant_snapshot":
        return (f"created {stats.get('snapshots_created', 0)}, "
                f"pruned {stats.get('snapshots_pruned', 0)}")
    if n == "task_index_repos":
        new = stats.get('new_files', 0)
        upd = stats.get('updated_files', 0)
        breakdown = f"{new} new, {upd} updated" if (new or upd) else f"{stats.get('files_indexed', 0)} files"
        return (f"{breakdown} indexed, "
                f"+{stats.get('chunks_added', 0)} chunks, "
                f"{stats.get('files_failed', 0)} failed")
    if n == "task_sync_sharepoint":
        parts = []
        for site, s in stats.items():
            if isinstance(s, dict):
                new = s.get('new', 0)
                upd = s.get('updated', 0)
                parts.append(f"{site}: {new} new, {upd} updated, "
                             f"{s.get('deleted', 0)} deleted, {s.get('errors', 0)}✗")
        return " | ".join(parts) if parts else "—"
    if n == "task_scan_qcodes":
        return f"{stats.get('dbs_found', 0)} DBs, +{stats.get('new_runs', 0)} runs"
    if n == "task_process_change_queue":
        return (f"{stats.get('total', 0)} entries "
                f"({stats.get('docs', 0)} docs, {stats.get('dbs', 0)} DBs)")
    if n == "task_orphan_cleanup":
        deleted = (stats.get("repo") or {}).get("deleted", 0)
        deleted += (stats.get("server") or {}).get("deleted", 0)
        return f"{deleted} orphans deleted"
    return str(stats)[:80] if stats else "—"


STATUS_ICON = {"ok": "✅", "fail": "❌", "timeout": "⏱️"}
STATUS_COLOR = {"ok": "green", "fail": "red", "timeout": "orange"}


def _build_summary_html(report: dict) -> str:
    date_str = report["run_date"]
    ok = report["tasks_ok"]
    total = report["tasks_total"]
    all_ok = ok == total

    try:
        from datetime import datetime
        start = datetime.fromisoformat(report["run_start"])
        end = datetime.fromisoformat(report["run_end"])
        runtime = _fmt_duration((end - start).total_seconds())
        start_str = start.strftime("%H:%M UTC")
    except Exception:
        runtime = "?"
        start_str = "?"

    icon = "✅" if all_ok else "❌"
    rows = ""
    for t in report["tasks"]:
        icon_t = STATUS_ICON.get(t["status"], "❓")
        name = t["name"].replace("task_", "")
        dur = _fmt_duration(t["duration_s"])
        detail = _task_detail(t)
        err = ""
        if t.get("error"):
            first = (t["error"] or "").splitlines()[0]
            err = f"<br><em style='color:red'>{first}</em>"
        rows += (f"<tr><td>{icon_t}</td><td><b>{name}</b></td>"
                 f"<td>{dur}</td><td>{detail}{err}</td></tr>")

    return (
        f"<h3>{icon} QNOE Nightly Report — {date_str}</h3>"
        f"<p>{ok}/{total} tasks succeeded &nbsp;·&nbsp; runtime {runtime} "
        f"&nbsp;·&nbsp; completed at {start_str}</p>"
        f"<table>"
        f"<tr><th>​</th><th>Task</th><th>Duration</th><th>Details</th></tr>"
        f"{rows}"
        f"</table>"
    )


def _build_errors_html(report: dict) -> str | None:
    lines: list[str] = []

    for t in report["tasks"]:
        files = t.get("failed_files") or []
        if files:
            lines.append(f"<b>{t['name']} — {len(files)} failed file(s):</b><ul>")
            for f in files[:30]:
                lines.append(f"<li><code>{f}</code></li>")
            if len(files) > 30:
                lines.append(f"<li><em>… and {len(files) - 30} more</em></li>")
            lines.append("</ul>")

    for t in report["tasks"]:
        warns = t.get("warnings") or []
        if warns:
            lines.append(f"<b>{t['name']} — warnings:</b><ul>")
            for w in warns[:15]:
                lines.append(f"<li>{w}</li>")
            lines.append("</ul>")

    for t in report["tasks"]:
        if t["status"] != "ok" and t.get("error"):
            err = (t["error"] or "")
            if len(err) > 2000:
                err = err[:1000] + "\n… (truncated) …\n" + err[-800:]
            lines.append(f"<b>{t['name']} [{t['status'].upper()}]:</b><pre>{err}</pre>")

    if not lines:
        return None

    date_str = report["run_date"]
    txt_path = REPORT_TXT_DIR / f"nightly_report_{date_str}.txt"
    lines.append(f"<p><em>Full report on DGX: <code>{txt_path}</code></em></p>")
    return "\n".join(lines)


def post_report(report: dict, secrets: dict) -> None:
    token = _authenticate(secrets)

    # Prefer channel delivery (REPORT_TEAM_ID + REPORT_CHANNEL_ID)
    team_id = secrets.get("REPORT_TEAM_ID", "").strip()
    channel_id = secrets.get("REPORT_CHANNEL_ID", "").strip()

    if team_id and channel_id:
        logger.info("Posting to channel: team=%s channel=%s", team_id, channel_id)
        _send_channel_message(token, team_id, channel_id, _build_summary_html(report))
        logger.info("Sent summary to channel")

        errors_html = _build_errors_html(report)
        if errors_html:
            _send_channel_message(token, team_id, channel_id, errors_html)
            logger.info("Sent error details to channel")
        return

    # Fallback: DM delivery
    chat_id = secrets.get("REPORT_CHAT_ID", "").strip()
    if chat_id:
        logger.info("Using stored chat ID: %s", chat_id)
    else:
        recipient = secrets.get("REPORT_TO_EMAIL", "") or secrets.get("REPORT_RECIPIENT_ID", "")
        if not recipient:
            raise ValueError(
                "Set REPORT_TEAM_ID + REPORT_CHANNEL_ID (channel) or "
                "REPORT_CHAT_ID / REPORT_TO_EMAIL (DM) in "
                "/opt/qnoe-agent/secrets/report.env"
            )
        bot_upn = _get_bot_upn(token)
        logger.info("Bot UPN: %s", bot_upn)
        chat_id = _get_or_create_chat(token, bot_upn, recipient)
        logger.info("Chat ID: %s", chat_id)

    _send_chat_message(token, chat_id, _build_summary_html(report))
    logger.info("Sent summary message")

    errors_html = _build_errors_html(report)
    if errors_html:
        _send_chat_message(token, chat_id, errors_html)
        logger.info("Sent error details message")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Send nightly report as Teams DM")
    parser.add_argument("--dry-run", action="store_true", help="Print messages without sending")
    args = parser.parse_args()

    if not REPORT_JSON.exists():
        logger.error("Report not found: %s", REPORT_JSON)
        sys.exit(1)

    report = json.loads(REPORT_JSON.read_text())
    secrets = _load_secrets()

    if args.dry_run:
        print("=== SUMMARY ===")
        print(_build_summary_html(report))
        err = _build_errors_html(report)
        if err:
            print("\n=== ERRORS ===")
            print(err)
        return

    post_report(report, secrets)
    logger.info("Done.")


if __name__ == "__main__":
    main()
