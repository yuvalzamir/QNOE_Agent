"""QNOE access-control plugin — notify-and-approve for Teams users.

When a user who is not authorized DMs the bot, instead of the gateway's silent
drop this plugin:
  1. Posts an access request to the "Agent Logs" Teams channel, with a ready
     to-paste ``/approve <user_id>`` command.
  2. Replies once to the user that their request was forwarded.
  3. Drops the message (returns ``{"action": "skip"}``).

Admins approve/deny by DMing the bot:
  /pending            — list outstanding access requests
  /approve <user_id>  — grant access (persisted in the pairing store)
  /deny <user_id>     — reject + silence (no more notifications)
  /revoke <user_id>   — remove a previously approved user
  /authz              — usage help

Security model — defense in depth:
  * The gateway core is the REAL enforcement point. With
    ``GATEWAY_ALLOW_ALL_USERS=false`` and ``GATEWAY_ALLOWED_USERS`` set to the
    permanent members, ``GatewayRunner._is_user_authorized`` denies every
    non-listed / non-paired sender regardless of this plugin. If this hook
    fails to load or raises, unauthorized users are STILL blocked — they just
    get silence instead of the friendly flow.
  * "Approved" users live in Hermes' native pairing store
    (``teams_polling-approved.json``), which ``_is_user_authorized`` already
    honors via ``pairing_store.is_approved(...)``. The running gateway re-reads
    that file on every message, so /approve takes effect with no restart.
  * Admin commands are gated by SENDER user_id (``QNOE_ADMIN_USER_IDS``), never
    by trusting message text.

Runtime note: ``invoke_hook`` calls the callback synchronously (``ret =
cb(**kwargs)``, not awaited), but sending Teams messages / posting to the
channel are coroutines. We are inside the gateway's running event loop, so all
network I/O is scheduled with ``loop.create_task`` and the hook returns
immediately.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import msal

logger = logging.getLogger(__name__)

PLATFORM = "teams_polling"

# Microsoft Graph — same app registration used by the nightly report poster.
GRAPH = "https://graph.microsoft.com/v1.0"
APP_ID = "108a03c5-e265-4ab6-a5ea-9c902fd527d4"
TENANT_ID = "f78a768a-22ae-4432-9eb4-55ce4b73c8c3"

# Bot credentials + channel IDs live in these secret files (read directly; the
# gateway env only sources teams.env). report.env holds REPORT_TEAM_ID /
# REPORT_CHANNEL_ID; sharepoint.env / teams.env hold the service-account creds.
SECRETS_FILES = (
    "/opt/qnoe-agent/secrets/report.env",
    "/opt/qnoe-agent/secrets/sharepoint.env",
    "/opt/qnoe-agent/secrets/teams.env",
)

# Fallback admins if QNOE_ADMIN_USER_IDS is unset: Yuval Zamir + Frank Koppens.
_DEFAULT_ADMINS = {
    "862ec907-3e65-4c00-aa0c-02948656ae7f",  # Yuval Zamir
    "1ce94aba-44e9-43ce-863d-42ff77cc277c",  # Frank Koppens (PI)
}

# Don't re-notify the channel about the same pending user more often than this.
RENOTIFY_WINDOW_S = 6 * 3600

USER_ACK_TEXT = (
    "Thanks for reaching out! You're not on the QNOE lab agent's access list "
    "yet, so I've forwarded your request to the lab admins. You'll be able to "
    "use me once you're approved."
)

# Strong refs to in-flight tasks (asyncio only holds weak refs).
_bg_tasks: set[asyncio.Task] = set()

# Cached Graph token for channel posting (.default scope).
_token: dict[str, Any] = {"value": None, "exp": 0.0}
_token_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Admin set + request-queue state (pending / denied)
# --------------------------------------------------------------------------- #
def _admins() -> set[str]:
    raw = os.environ.get("QNOE_ADMIN_USER_IDS", "").strip()
    if raw:
        return {x.strip() for x in raw.split(",") if x.strip()}
    return set(_DEFAULT_ADMINS)


def _state_path() -> Path:
    home = os.environ.get("HERMES_HOME", "/opt/qnoe-agent/hermes")
    d = Path(home) / "pairing"
    d.mkdir(parents=True, exist_ok=True)
    return d / "qnoe_authz_state.json"


def _load_state() -> dict:
    try:
        st = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        st = {}
    st.setdefault("pending", {})
    st.setdefault("denied", {})
    return st


def _save_state(st: dict) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Graph / channel notification (async)
# --------------------------------------------------------------------------- #
def _cred_files() -> list[str]:
    """Secret files to scan. Under the B7 sandbox /opt/qnoe-agent/secrets is
    InaccessiblePaths and teams.env is delivered via $CREDENTIALS_DIRECTORY;
    the direct paths still work under the bare (rollback) unit. In practice the
    values also arrive via os.environ (start_hermes.sh sources teams.env and
    exports REPORT_TEAM_ID/REPORT_CHANNEL_ID), which wins below."""
    files = list(SECRETS_FILES)
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        for name in ("report.env", "sharepoint.env", "teams.env"):
            files.append(str(Path(cred_dir) / name))
    return files


def _load_secrets() -> dict:
    merged: dict[str, str] = {}
    for path in _cred_files():
        try:
            for line in Path(path).read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    merged.setdefault(k.strip(), v.strip())
        except OSError:
            pass
    merged.update(os.environ)
    return merged


def _acquire_token_sync() -> dict:
    s = _load_secrets()
    user = s.get("SHAREPOINT_USERNAME") or s.get("TEAMS_USERNAME", "")
    pwd = s.get("SHAREPOINT_PASSWORD") or s.get("TEAMS_PASSWORD", "")
    app = msal.PublicClientApplication(
        APP_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
    )
    return app.acquire_token_by_username_password(
        username=user, password=pwd, scopes=["https://graph.microsoft.com/.default"]
    )


async def _get_token() -> str:
    async with _token_lock:
        if _token["value"] and time.time() < _token["exp"] - 60:
            return _token["value"]
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, _acquire_token_sync)
        if "access_token" not in res:
            raise RuntimeError(f"token: {str(res.get('error_description', res))[:200]}")
        _token["value"] = res["access_token"]
        _token["exp"] = time.time() + res.get("expires_in", 3600)
        return _token["value"]


async def _post_channel(html_body: str) -> None:
    s = _load_secrets()
    team = s.get("REPORT_TEAM_ID", "").strip()
    chan = s.get("REPORT_CHANNEL_ID", "").strip()
    if not team or not chan:
        logger.warning(
            "qnoe_authz: REPORT_TEAM_ID/REPORT_CHANNEL_ID unset — cannot post "
            "access-request notification"
        )
        return
    token = await _get_token()
    url = f"{GRAPH}/teams/{team}/channels/{chan}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"body": {"contentType": "html", "content": html_body}}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=body, headers=headers) as r:
            if r.status >= 300:
                logger.warning(
                    "qnoe_authz: channel post failed %s: %s",
                    r.status, (await r.text())[:200],
                )


def _notify_html(name: str, uid: str) -> str:
    safe_name = _html.escape(name) if name else "(unknown user)"
    return (
        "🔔 <b>QNOE agent — access request</b><br>"
        f"<b>{safe_name}</b> (id <code>{uid}</code>) messaged the agent and is "
        "not on the access list.<br>"
        f"To grant access, DM the bot: <code>/approve {uid}</code>"
        f" &nbsp;·&nbsp; to reject: <code>/deny {uid}</code>"
    )


async def _notify_and_ack(adapter: Any, chat_id: Optional[str], name: str, uid: str) -> None:
    try:
        await _post_channel(_notify_html(name, uid))
    except Exception as exc:  # pragma: no cover - network path
        logger.warning("qnoe_authz: notify failed: %s", exc)
    if adapter is not None and chat_id:
        try:
            await adapter.send(chat_id, USER_ACK_TEXT)
        except Exception as exc:  # pragma: no cover - network path
            logger.warning("qnoe_authz: user ack failed: %s", exc)


def _schedule(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# --------------------------------------------------------------------------- #
# Admin commands
# --------------------------------------------------------------------------- #
def _handle_admin_command(gateway: Any, text: str) -> Optional[str]:
    """Handle an admin /command. Returns a reply string, or None if *text* is
    not one of our commands (so /new, /help, etc. still reach Hermes)."""
    parts = text.split()
    cmd = parts[0].lower()
    store = getattr(gateway, "pairing_store", None)

    if cmd == "/authz":
        return (
            "QNOE access commands (admins only):\n"
            "  /pending            list outstanding access requests\n"
            "  /approve <user_id>  grant access\n"
            "  /deny <user_id>     reject + silence\n"
            "  /revoke <user_id>   remove a previously approved user"
        )

    if cmd == "/pending":
        st = _load_state()
        pend = st.get("pending", {})
        if not pend:
            return "No pending access requests."
        lines = ["Pending access requests:"]
        for uid, info in pend.items():
            nm = info.get("name") or "(no name)"
            lines.append(f"  • {nm}  —  id {uid}  →  /approve {uid}")
        return "\n".join(lines)

    if cmd == "/approve":
        if len(parts) < 2:
            return "Usage: /approve <user_id>"
        uid = parts[1]
        if store is None:
            return "Error: pairing store unavailable."
        st = _load_state()
        name = (st.get("pending", {}).get(uid, {}) or {}).get("name", "")
        try:
            with store._lock:
                store._approve_user(PLATFORM, uid, name)
        except Exception as exc:
            return f"Approve failed: {exc}"
        st.get("pending", {}).pop(uid, None)
        st.get("denied", {}).pop(uid, None)
        _save_state(st)
        return f"✅ Approved {name or uid} — access granted (effective immediately)."

    if cmd == "/deny":
        if len(parts) < 2:
            return "Usage: /deny <user_id>"
        uid = parts[1]
        st = _load_state()
        name = (st.get("pending", {}).get(uid, {}) or {}).get("name", "")
        st.setdefault("denied", {})[uid] = {"name": name, "denied_at": time.time()}
        st.get("pending", {}).pop(uid, None)
        _save_state(st)
        return f"🚫 Denied {name or uid} — they'll be silently ignored from now on."

    if cmd == "/revoke":
        if len(parts) < 2:
            return "Usage: /revoke <user_id>"
        uid = parts[1]
        if store is None:
            return "Error: pairing store unavailable."
        removed = store.revoke(PLATFORM, uid)
        st = _load_state()
        st.setdefault("denied", {})[uid] = {"name": "", "denied_at": time.time()}
        st.get("pending", {}).pop(uid, None)
        _save_state(st)
        base = "removed from the approved list" if removed else "was not in the approved list"
        return (
            f"↩️ {uid} {base}. (Note: permanent members in GATEWAY_ALLOWED_USERS "
            "can't be revoked here — edit the start script + restart.)"
        )

    return None  # not one of our commands


# --------------------------------------------------------------------------- #
# Pre-dispatch hook
# --------------------------------------------------------------------------- #
def _pre_gateway_dispatch(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **_: Any,
) -> Optional[dict]:
    try:
        source = getattr(event, "source", None)
        if source is None or gateway is None:
            return None
        platform = getattr(source, "platform", None)
        if platform is None or getattr(platform, "value", "") != PLATFORM:
            return None  # not our platform

        uid = getattr(source, "user_id", None) or ""
        chat_id = getattr(source, "chat_id", None)
        text = (getattr(event, "text", "") or "").strip()
        adapter = (getattr(gateway, "adapters", {}) or {}).get(platform)

        # 1. Admin commands (gated by sender user_id, not by text).
        if uid and uid in _admins() and text.startswith("/"):
            reply = _handle_admin_command(gateway, text)
            if reply is not None:
                if adapter is not None and chat_id:
                    _schedule(adapter.send(chat_id, reply))
                return {"action": "skip", "reason": "qnoe authz admin command"}
            # else: not our command — fall through so /new, /help still work.

        # 2. Authorized? (permanent floor via GATEWAY_ALLOWED_USERS, or paired)
        try:
            if gateway._is_user_authorized(source):
                return None
        except Exception as exc:
            logger.warning("qnoe_authz: _is_user_authorized raised: %s", exc)
            return None  # let the gateway's own auth path run (still denies)

        # 3. Previously denied → silent drop, no re-notify.
        st = _load_state()
        if uid and uid in st.get("denied", {}):
            return {"action": "skip", "reason": "denied user"}

        # 4. Unknown user → queue + notify channel (rate-limited) + ack user.
        if uid:
            now = time.time()
            pend = st.setdefault("pending", {})
            entry = pend.get(uid) or {}
            name = getattr(source, "user_name", None) or entry.get("name", "") or ""
            last_notified = entry.get("last_notified", 0)
            do_notify = (now - last_notified) >= RENOTIFY_WINDOW_S
            pend[uid] = {
                "name": name,
                "first_seen": entry.get("first_seen", now),
                "last_notified": now if do_notify else last_notified,
            }
            _save_state(st)
            if do_notify:
                logger.warning(
                    "qnoe_authz: access request from %s (id=%s) — notifying channel",
                    name or "(unknown)", uid,
                )
                _schedule(_notify_and_ack(adapter, chat_id, name, uid))

        return {"action": "skip", "reason": "unauthorized (pending approval)"}
    except Exception as exc:
        logger.warning("qnoe_authz: hook error: %s", exc)
        return None


def register(ctx) -> None:
    """Register the pre-dispatch access-control hook with Hermes Agent."""
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
