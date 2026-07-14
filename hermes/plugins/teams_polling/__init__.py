"""Microsoft Teams polling adapter plugin for Hermes Agent.

Polls the Graph API for new messages instead of requiring inbound webhooks.
Designed for on-premises deployments (like the DGX Spark) that have no
public IP.

Authentication: MSAL ROPC (username + password) via a service account.

Required env vars:
  TEAMS_TENANT_ID  — Azure AD tenant ID
  TEAMS_CLIENT_ID  — Azure app registration client ID
  TEAMS_USERNAME   — service account UPN
  TEAMS_PASSWORD   — service account password

Polling strategy:
  Active (message in last 5 min):  3 s
  Idle:                            10 s
  Chat list refresh:               every 5 min
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import aiohttp
import msal

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = [
    "https://graph.microsoft.com/Chat.Read",
    "https://graph.microsoft.com/Chat.ReadWrite",
    "https://graph.microsoft.com/ChatMessage.Send",
]

DEFAULT_ACTIVE_POLL = 3
DEFAULT_IDLE_POLL = 10
DEFAULT_ACTIVE_WINDOW = 300
DEFAULT_CHAT_REFRESH = 300
MAX_SEEN_IDS = 5000

# Strip HTML tags from Teams message content
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags from Teams message body content."""
    return _HTML_TAG_RE.sub("", text).strip()


def check_teams_polling() -> bool:
    """Check if required env vars are set."""
    return all(
        os.environ.get(v)
        for v in ("TEAMS_TENANT_ID", "TEAMS_CLIENT_ID", "TEAMS_USERNAME", "TEAMS_PASSWORD")
    )


class TeamsPollingAdapter(BasePlatformAdapter):
    """Microsoft Teams adapter using Graph API polling."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("teams_polling"))
        extra = config.extra or {}

        self._tenant_id = os.environ.get("TEAMS_TENANT_ID", "")
        self._client_id = os.environ.get("TEAMS_CLIENT_ID", "")
        self._username = os.environ.get("TEAMS_USERNAME", "")
        self._password = os.environ.get("TEAMS_PASSWORD", "")

        self._active_poll = int(extra.get("poll_interval_active", DEFAULT_ACTIVE_POLL))
        self._idle_poll = int(extra.get("poll_interval_idle", DEFAULT_IDLE_POLL))
        self._active_window = int(extra.get("active_window", DEFAULT_ACTIVE_WINDOW))
        self._chat_refresh_interval = int(extra.get("chat_refresh_interval", DEFAULT_CHAT_REFRESH))

        self._app: msal.PublicClientApplication | None = None
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._me_id: str = ""
        self._me_name: str = ""
        self._startup_ts: datetime = datetime.min.replace(tzinfo=timezone.utc)

        self._chat_ids: set[str] = set()
        self._chat_refresh_at: float = 0.0

        self._seen_msg_ids: deque[str] = deque(maxlen=MAX_SEEN_IDS)
        self._seen_set: set[str] = set()

        self._last_message_time: float = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None

    # -- Authentication ------------------------------------------------------

    def _init_msal(self) -> None:
        self._app = msal.PublicClientApplication(
            self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
        )

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._acquire_token_sync)

        if "access_token" not in result:
            error = result.get("error_description") or result.get("error", "unknown")
            raise RuntimeError(f"MSAL token acquisition failed: {error}")

        self._token = result["access_token"]
        self._token_expires = time.time() + result.get("expires_in", 3600)
        return self._token

    def _acquire_token_sync(self) -> dict:
        accounts = self._app.get_accounts(username=self._username)
        if accounts:
            result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
            if result and "access_token" in result:
                return result
        return self._app.acquire_token_by_username_password(
            username=self._username,
            password=self._password,
            scopes=SCOPES,
        )

    async def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {await self._get_token()}",
            "Content-Type": "application/json",
        }

    # -- HTTP helpers --------------------------------------------------------

    async def _get(self, url: str) -> dict:
        for attempt in range(3):
            async with self._session.get(url, headers=await self._headers()) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    logger.warning("Teams rate limited — sleeping %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return await resp.json()
        raise RuntimeError(f"GET {url} failed after retries")

    async def _post(self, url: str, body: dict) -> dict:
        for attempt in range(3):
            async with self._session.post(
                url, json=body, headers=await self._headers()
            ) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return await resp.json()
        raise RuntimeError(f"POST {url} failed after retries")

    # -- Bootstrap -----------------------------------------------------------

    async def _bootstrap(self) -> None:
        me = await self._get(f"{GRAPH_BASE}/me?$select=id,displayName")
        self._me_id = me["id"]
        self._me_name = me.get("displayName", "")
        self._startup_ts = datetime.now(timezone.utc)
        logger.info(
            "Teams: authenticated as %s (id=%s), ignoring messages before %s",
            self._me_name, self._me_id, self._startup_ts.isoformat(),
        )
        await self._refresh_chat_list()

    async def _refresh_chat_list(self) -> None:
        try:
            data = await self._get(
                f"{GRAPH_BASE}/me/chats?$select=id,chatType&$top=50"
            )
            self._chat_ids = {
                c["id"]
                for c in data.get("value", [])
                if c.get("chatType") == "oneOnOne"
            }
            self._chat_refresh_at = time.time() + self._chat_refresh_interval
            logger.debug("Teams: tracking %d DM chats", len(self._chat_ids))
        except Exception as exc:
            logger.warning("Teams chat list refresh failed: %s", exc)

    # -- Deduplication -------------------------------------------------------

    def _is_new(self, msg_id: str) -> bool:
        if msg_id in self._seen_set:
            return False
        if len(self._seen_msg_ids) == MAX_SEEN_IDS:
            evicted = self._seen_msg_ids[0]
            self._seen_set.discard(evicted)
        self._seen_msg_ids.append(msg_id)
        self._seen_set.add(msg_id)
        return True

    def _after_startup(self, iso_str: str) -> bool:
        try:
            ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return ts > self._startup_ts
        except (ValueError, TypeError):
            return False

    # -- Polling -------------------------------------------------------------

    def _msg_to_event(self, msg: dict, chat_id: str) -> MessageEvent | None:
        """Convert a Graph API message dict to a Hermes MessageEvent."""
        if msg.get("messageType") != "message":
            return None
        msg_id = msg.get("id", "")
        if not self._is_new(msg_id):
            return None
        if not self._after_startup(msg.get("createdDateTime", "")):
            return None

        sender = (msg.get("from") or {}).get("user") or {}
        sender_id = sender.get("id", "")
        if sender_id == self._me_id:
            return None

        body = (msg.get("body") or {}).get("content", "")
        text = _strip_html(body).strip()
        if not text:
            return None

        source = SessionSource(
            platform=Platform("teams_polling"),
            chat_id=chat_id,
            chat_type="dm",
            user_id=sender_id,
            user_name=sender.get("displayName"),
            message_id=msg_id,
        )

        # MessageEvent is a dataclass whose first field `text` is required —
        # MessageEvent() with no args raises TypeError and poisons the poll
        # cycle (every poll retries the same message forever).
        event = MessageEvent(text)
        event.message_type = MessageType.TEXT
        event.source = source
        event.message_id = msg_id
        event.timestamp = datetime.now(timezone.utc)
        return event

    async def _poll_chat(self, chat_id: str) -> list[MessageEvent]:
        try:
            data = await self._get(
                f"{GRAPH_BASE}/chats/{chat_id}/messages"
                f"?$top=10&$orderby=createdDateTime desc"
            )
        except Exception as exc:
            logger.warning("Teams message fetch error for chat %s: %s", chat_id, exc)
            return []

        events = []
        for msg in data.get("value", []):
            event = self._msg_to_event(msg, chat_id)
            if event:
                events.append(event)
        return events

    async def _poll_cycle(self) -> None:
        if time.time() >= self._chat_refresh_at:
            await self._refresh_chat_list()

        for chat_id in list(self._chat_ids):
            for event in await self._poll_chat(chat_id):
                self._last_message_time = time.time()
                try:
                    await self.handle_message(event)
                except Exception as exc:
                    logger.error(
                        "Teams handle_message error for chat %s: %s",
                        chat_id, exc, exc_info=True,
                    )

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:
                logger.error("Teams poll cycle error: %s", exc, exc_info=True)
            idle = (time.time() - self._last_message_time) > self._active_window
            await asyncio.sleep(self._idle_poll if idle else self._active_poll)

    # -- BasePlatformAdapter interface ---------------------------------------

    async def connect(self) -> bool:
        # trust_env: honor HTTP(S)_PROXY + SSL_CERT_FILE from the environment.
        # Required inside the OpenShell sandbox (B7-OS), where ALL egress goes
        # through an injected L7 proxy and there is no direct DNS — aiohttp
        # defaults to trust_env=False and would fail with "Temporary failure in
        # name resolution". No-op outside the sandbox (no proxy env set).
        self._session = aiohttp.ClientSession(trust_env=True)
        self._init_msal()
        try:
            await self._bootstrap()
        except Exception as exc:
            logger.error("Teams bootstrap failed: %s", exc)
            await self._session.close()
            return False

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Teams polling adapter connected")
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        logger.info("Teams polling adapter disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        try:
            result = await self._post(
                f"{GRAPH_BASE}/chats/{chat_id}/messages",
                {"body": {"content": content, "contentType": "text"}},
            )
            return SendResult(
                success=True,
                message_id=result.get("id"),
            )
        except Exception as exc:
            logger.error("Teams send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # Teams Graph API doesn't support typing indicators for bots
        pass

    async def get_chat_info(self, chat_id: str) -> dict:
        try:
            data = await self._get(
                f"{GRAPH_BASE}/chats/{chat_id}?$select=id,chatType,topic"
            )
            return {
                "name": data.get("topic", chat_id),
                "type": "dm" if data.get("chatType") == "oneOnOne" else "group",
            }
        except Exception:
            return {"name": chat_id, "type": "dm"}


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Register the Teams polling platform adapter."""
    ctx.register_platform(
        name="teams_polling",
        label="Teams (Polling)",
        adapter_factory=lambda cfg: TeamsPollingAdapter(cfg),
        check_fn=check_teams_polling,
        required_env=["TEAMS_TENANT_ID", "TEAMS_CLIENT_ID", "TEAMS_USERNAME", "TEAMS_PASSWORD"],
        emoji="👥",
    )
