from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection


TRUE_VALUES = {"1", "true", "yes", "y", "on"}

BOT_ID: int | None = None
BOT_USERNAME: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def parse_owner_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in re.split(r"[\s,]+", raw.strip()):
        if not part:
            continue
        ids.add(int(part))
    return ids


def normalize_username(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("@"):
        value = value[1:]
    return value.lower()


def display_username(username: str | None) -> str:
    return f"@{username}" if username else "(no username)"


def normalize_chat_id(value: str | int) -> str:
    text = str(value).strip()
    if text.startswith("@"):
        return "@" + text[1:].lower()
    return text


def telegram_chat_id(value: str | int) -> str | int:
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def clean_doc(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: set[int]
    invite_code: str
    allow_self_service: bool
    public_url: str | None
    webhook_secret: str
    mongodb_uri: str
    mongodb_db: str
    request_timeout: float
    promote_can_post: bool
    promote_can_edit: bool
    promote_can_delete: bool
    allowed_updates: list[str]

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")

        mongodb_uri = os.getenv("MONGODB_URI", "").strip()
        if not mongodb_uri:
            raise RuntimeError("MONGODB_URI is required for persistent storage")

        public_url = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
        if public_url:
            public_url = public_url.rstrip("/")

        return cls(
            bot_token=bot_token,
            owner_ids=parse_owner_ids(os.getenv("OWNER_IDS", "")),
            invite_code=os.getenv("INVITE_CODE", "").strip(),
            allow_self_service=env_bool("ALLOW_SELF_SERVICE"),
            public_url=public_url,
            webhook_secret=re.sub(
                r"[^A-Za-z0-9_-]", "",
                os.getenv("WEBHOOK_SECRET", "").strip()
            )
            or re.sub(r"[^A-Za-z0-9_-]", "", secrets.token_urlsafe(24)),
            mongodb_uri=mongodb_uri,
            mongodb_db=os.getenv("MONGODB_DB", "bot_manager").strip()
            or "bot_manager",
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "20")),
            promote_can_post=env_bool("PROMOTE_CAN_POST_MESSAGES"),
            promote_can_edit=env_bool("PROMOTE_CAN_EDIT_MESSAGES"),
            promote_can_delete=env_bool("PROMOTE_CAN_DELETE_MESSAGES"),
            allowed_updates=["message", "channel_post", "my_chat_member"],
        )


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=8000)
        self.db = self.client[settings.mongodb_db]
        self.tenants: Collection = self.db["tenants"]
        self.bots: Collection = self.db["managed_bots"]
        self.channels: Collection = self.db["channels"]

    def init(self) -> None:
        self.client.admin.command("ping")
        self.tenants.create_index("owner_user_id", unique=True)
        self.bots.create_index(
            [("tenant_id", ASCENDING), ("bot_user_id", ASCENDING)], unique=True
        )

        self.channels.create_index(
            [("tenant_id", ASCENDING), ("chat_id", ASCENDING)], unique=True
        )
        # Drop legacy unique sparse indexes on username that cause E11000 errors
        # with null usernames (private channels/bots without @username)
        for name in ["tenant_id_1_username_1"]:
            for coll_name in ["channels", "managed_bots"]:
                try:
                    self.db[coll_name].drop_index(name)
                except Exception:
                    pass



        for owner_id in self.settings.owner_ids:
            self.ensure_tenant_id(owner_id, source="env_owner")
            for raw in re.split(r"[\s,]+", os.getenv("CHANNELS", "").strip()):
                if raw:
                    self.add_channel(str(owner_id), raw, title=None, username=None, added_by=None)

    def close(self) -> None:
        self.client.close()

    def ensure_tenant_id(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        tenant_id = str(user_id)
        now = utcnow()
        update = {
            "$set": clean_doc(
                {
                    "owner_user_id": int(user_id),
                    "username": normalize_username(username),
                    "first_name": first_name,
                    "active": True,
                    "source": source,
                    "updated_at": now,
                }
            ),
            "$setOnInsert": {
                "_id": tenant_id,
                "created_at": now,
            },
        }
        self.tenants.update_one({"_id": tenant_id}, update, upsert=True)
        tenant = self.get_tenant(tenant_id)
        if not tenant:
            raise RuntimeError("tenant upsert failed")
        return tenant

    def ensure_tenant_from_user(self, user: dict[str, Any], source: str) -> dict[str, Any]:
        return self.ensure_tenant_id(
            int(user["id"]),
            username=user.get("username"),
            first_name=user.get("first_name"),
            source=source,
        )

    def get_tenant(self, tenant_id: str | int | None) -> dict[str, Any] | None:
        if tenant_id is None:
            return None
        return self.tenants.find_one({"_id": str(tenant_id)})

    def get_tenant_for_user(self, user_id: int | None) -> dict[str, Any] | None:
        if user_id is None:
            return None
        return self.get_tenant(str(user_id))

    def tenant_is_active(self, tenant_id: str | int | None) -> bool:
        tenant = self.get_tenant(tenant_id)
        return bool(tenant and tenant.get("active"))

    def add_bot(
        self,
        tenant_id: str | int,
        bot_user_id: int,
        username: str | None,
        label: str | None,
        added_by: int | None,
    ) -> None:
        tenant_id = str(tenant_id)
        username = normalize_username(username)
        self.bots.update_one(
            {"tenant_id": tenant_id, "bot_user_id": int(bot_user_id)},
            {"$set": clean_doc(
                {
                    "tenant_id": tenant_id,
                    "bot_user_id": int(bot_user_id),
                    "username": username,
                    "label": label,
                    "added_by": added_by,
                    "updated_at": utcnow(),
                }
            ),
            "$setOnInsert": {"created_at": utcnow()}},
            upsert=True,
        )

    def remove_bot(self, tenant_id: str | int, target: str) -> int:
        tenant_id = str(tenant_id)
        target = target.strip()
        if re.fullmatch(r"\d+", target):
            query = {"tenant_id": tenant_id, "bot_user_id": int(target)}
        else:
            query = {"tenant_id": tenant_id, "username": normalize_username(target)}
        result = self.bots.delete_many(query)
        return int(result.deleted_count)

    def list_bots(self, tenant_id: str | int) -> list[dict[str, Any]]:
        cursor = self.bots.find({"tenant_id": str(tenant_id)}).sort("username", ASCENDING)
        return [dict(row) for row in cursor]

    def find_bot(self, tenant_id: str | int, target: str) -> dict[str, Any] | None:
        tenant_id = str(tenant_id)
        target = target.strip()
        if re.fullmatch(r"\d+", target):
            query = {"tenant_id": tenant_id, "bot_user_id": int(target)}
        else:
            query = {"tenant_id": tenant_id, "username": normalize_username(target)}
        return self.bots.find_one(query)

    def add_channel(
        self,
        tenant_id: str | int,
        chat_id: str | int,
        title: str | None,
        username: str | None,
        added_by: int | None,
    ) -> None:
        tenant_id = str(tenant_id)
        normalized = normalize_chat_id(chat_id)
        username = normalize_username(username)
        self.channels.update_one(
            {"tenant_id": tenant_id, "chat_id": normalized},
            {"$set": clean_doc(
                {
                    "tenant_id": tenant_id,
                    "chat_id": normalized,
                    "title": title,
                    "username": username,
                    "added_by": added_by,
                    "updated_at": utcnow(),
                }
            ),
            "$setOnInsert": {"created_at": utcnow()}},
            upsert=True,
        )

    def remove_channel(self, tenant_id: str | int, target: str) -> int:
        tenant_id = str(tenant_id)
        target = normalize_chat_id(target)
        query: dict[str, Any]
        if target.startswith("@"):
            query = {
                "tenant_id": tenant_id,
                "$or": [
                    {"chat_id": target},
                    {"username": normalize_username(target)},
                ],
            }
        else:
            query = {"tenant_id": tenant_id, "chat_id": target}
        result = self.channels.delete_many(query)
        return int(result.deleted_count)

    def list_channels(self, tenant_id: str | int) -> list[dict[str, Any]]:
        cursor = self.channels.find({"tenant_id": str(tenant_id)}).sort("title", ASCENDING)
        return [dict(row) for row in cursor]


class TelegramAPIError(Exception):
    def __init__(
        self,
        method: str,
        description: str,
        error_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(description)
        self.method = method
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.request_timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> Any:
        url = f"https://api.telegram.org/bot{token or self.settings.bot_token}/{method}"
        response = await self.client.post(url, json=payload or {})
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramAPIError(method, response.text, response.status_code) from exc

        if not data.get("ok"):
            params = data.get("parameters") or {}
            raise TelegramAPIError(
                method=method,
                description=data.get("description", "Telegram API error"),
                error_code=data.get("error_code"),
                retry_after=params.get("retry_after"),
            )
        return data.get("result")


settings = Settings.from_env()
storage = Storage(settings)
telegram = TelegramClient(settings)
app = FastAPI(title="Telegram Bot Administrator", version="2.0.0")


def user_id_from_message(message: dict[str, Any]) -> int | None:
    user = message.get("from") or {}
    user_id = user.get("id")
    return int(user_id) if user_id is not None else None


def active_tenant_for_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user or user.get("id") is None:
        return None
    user_id = int(user["id"])
    tenant = storage.get_tenant_for_user(user_id)
    if tenant and tenant.get("active"):
        return tenant
    if user_id in settings.owner_ids:
        return storage.ensure_tenant_from_user(user, source="env_owner")
    return None


def invite_matches(args: list[str]) -> bool:
    if settings.allow_self_service:
        return True
    if not settings.invite_code or not args:
        return False
    return secrets.compare_digest(args[0], settings.invite_code)


def command_parts(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


def extract_forwarded_user(message: dict[str, Any]) -> dict[str, Any] | None:
    origin = message.get("forward_origin") or {}
    if origin.get("type") == "user" and origin.get("sender_user"):
        return origin["sender_user"]
    if message.get("forward_from"):
        return message["forward_from"]
    return None


def extract_forwarded_channel(message: dict[str, Any]) -> dict[str, Any] | None:
    origin = message.get("forward_origin") or {}
    if origin.get("type") == "channel" and origin.get("chat"):
        return origin["chat"]
    if message.get("forward_from_chat"):
        return message["forward_from_chat"]
    return None


def extract_identity_source(message: dict[str, Any]) -> dict[str, Any] | None:
    reply = message.get("reply_to_message")
    if reply:
        return extract_forwarded_user(reply) or reply.get("from")
    return extract_forwarded_user(message)


def extract_channel_source(message: dict[str, Any]) -> dict[str, Any] | None:
    reply = message.get("reply_to_message")
    if reply:
        return extract_forwarded_channel(reply)
    return extract_forwarded_channel(message)


async def send_message(
    chat_id: str | int,
    text: str,
    reply_to_message_id: int | None = None,
) -> None:
    chunks = [text[i : i + 3800] for i in range(0, len(text), 3800)] or [text]
    for chunk in chunks:
        payload: dict[str, Any] = {
            "chat_id": telegram_chat_id(chat_id),
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        await telegram.call("sendMessage", payload)


async def notify_super_owners(text: str) -> None:
    for owner_id in settings.owner_ids:
        try:
            await send_message(owner_id, text)
        except TelegramAPIError:
            pass


def promotion_rights() -> dict[str, bool]:
    rights = {
        "is_anonymous": False,
        "can_manage_chat": True,
        "can_delete_messages": settings.promote_can_delete,
        "can_manage_video_chats": False,
        "can_restrict_members": False,
        "can_promote_members": False,
        "can_change_info": False,
        "can_invite_users": True,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_post_messages": settings.promote_can_post,
        "can_edit_messages": settings.promote_can_edit,
        "can_pin_messages": False,
        "can_manage_topics": False,
        "can_manage_direct_messages": False,
    }
    if any(
        rights[key]
        for key in (
            "can_delete_messages",
            "can_post_messages",
            "can_edit_messages",
            "can_change_info",
            "can_invite_users",
        )
    ):
        rights["can_manage_chat"] = True
    return rights


def demotion_rights() -> dict[str, bool]:
    return {key: False for key in promotion_rights()}


async def promote_one(chat_id: str, bot: dict[str, Any]) -> tuple[bool, str]:
    payload = {
        "chat_id": telegram_chat_id(chat_id),
        "user_id": int(bot["bot_user_id"]),
        **promotion_rights(),
    }
    try:
        await telegram.call("promoteChatMember", payload)
        return True, f"ok {display_username(bot.get('username'))} ({bot['bot_user_id']})"
    except TelegramAPIError as exc:
        desc = exc.description or ""
        if "CHAT_ADMIN_INVITE_REQUIRED" in desc:
            return (
                False,
                f"skip {display_username(bot.get('username'))} ({bot['bot_user_id']}): "
                f"not a member of this channel. Add the bot manually, "
                f"then re-sync.",
            )
        return (
            False,
            f"fail {display_username(bot.get('username'))} ({bot['bot_user_id']}): {exc.description}",
        )


async def demote_one(chat_id: str, bot: dict[str, Any]) -> tuple[bool, str]:
    payload = {
        "chat_id": telegram_chat_id(chat_id),
        "user_id": int(bot["bot_user_id"]),
        **demotion_rights(),
    }
    try:
        await telegram.call("promoteChatMember", payload)
        return True, f"ok {display_username(bot.get('username'))} ({bot['bot_user_id']})"
    except TelegramAPIError as exc:
        return (
            False,
            f"fail {display_username(bot.get('username'))} ({bot['bot_user_id']}): {exc.description}",
        )


async def sync_channel(tenant_id: str, chat_id: str) -> list[str]:
    bots = storage.list_bots(tenant_id)
    if not bots:
        return ["No registered bots yet. Use /addbot first."]

    lines = [f"<b>Syncing {h(chat_id)}</b>"]
    for bot in bots:
        ok, line = await promote_one(chat_id, bot)
        lines.append(("[OK] " if ok else "[FAIL] ") + h(line))
        await asyncio.sleep(0.15)
    return lines


def render_bots(tenant_id: str) -> str:
    bots = storage.list_bots(tenant_id)
    if not bots:
        return "No bots registered."
    lines = ["<b>Registered bots</b>"]
    for bot in bots:
        label = f" - {h(bot['label'])}" if bot.get("label") else ""
        lines.append(
            f"{display_username(bot.get('username'))} | <code>{bot['bot_user_id']}</code>{label}"
        )
    return "\n".join(lines)


def render_channels(tenant_id: str) -> str:
    channels = storage.list_channels(tenant_id)
    if not channels:
        return "No channels saved. Admin me in a channel or use /addchannel."
    lines = ["<b>Saved channels</b>"]
    for channel in channels:
        username = display_username(channel.get("username"))
        title = f" - {h(channel['title'])}" if channel.get("title") else ""
        lines.append(f"<code>{h(channel['chat_id'])}</code> {username}{title}")
    return "\n".join(lines)


def help_text(active: bool = True) -> str:
    if not active:
        return "\n".join(
            [
                "<b>Bot Administration</b>",
                "",
                "This shared manager keeps every user's setup separate.",
                "Send /invite CODE to create your own workspace.",
                "/whoami shows your Telegram user ID.",
            ]
        )

    return "\n".join(
        [
            "<b>Bot Administration</b>",
            "",
            "Your workspace is isolated from other users.",
            "",
            "Account:",
            "/workspace - show your workspace details",
            "",
            "Admin setup:",
            "/whoami - show your Telegram user ID",
            "/addbot 123456789 @SomeBot - register a file bot by numeric ID",
            "/addbot - reply to a forwarded bot message to register it",
            "/removebot @SomeBot",
            "/bots",
            "",
            "Channel setup:",
            "/addchannel @channel - save a channel",
            "/removechannel @channel",
            "/channels",
            "/checkadmin @channel - check my admin rights",
            "",
            "Actions:",
            "/sync @channel - promote all your registered bots in that channel",
            "/syncall - promote all your registered bots in your saved channels",
            "/admins @channel - list bot admins I can see",
            "/demote @channel @SomeBot - remove one registered bot's admin rights",
            "",
        ]
    )


async def activate_user(
    message: dict[str, Any],
    source: str,
    reply_id: int | None,
) -> dict[str, Any] | None:
    chat_id = message["chat"]["id"]
    from_user = message.get("from") or {}
    if not from_user.get("id"):
        await send_message(chat_id, "I could not read your user ID.", reply_id)
        return None
    tenant = storage.ensure_tenant_from_user(from_user, source=source)
    return tenant


async def handle_admin_command(
    message: dict[str, Any],
    command: str,
    args: list[str],
) -> None:
    chat_id = message["chat"]["id"]
    from_user = message.get("from") or {}
    owner_id = user_id_from_message(message)
    reply_id = message.get("message_id")

    if command in {"/help", "/start"}:
        tenant = active_tenant_for_user(from_user)
        if command == "/start" and not tenant and invite_matches(args):
            await activate_user(message, "invite", reply_id)
            return
        await send_message(chat_id, help_text(active=bool(tenant)), reply_id)
        return

    if command == "/invite":
        if invite_matches(args):
            await activate_user(message, "invite", reply_id)
            return
        await send_message(
            chat_id,
            "Invalid invite code. Ask the bot owner for the code, or use /whoami.",
            reply_id,
        )
        return

    tenant = active_tenant_for_user(from_user)
    if not tenant:
        await send_message(chat_id, help_text(active=False), reply_id)
        return
    tenant_id = str(tenant["_id"])

    if command == "/workspace":
        bot_count = len(storage.list_bots(tenant_id))
        channel_count = len(storage.list_channels(tenant_id))

        await send_message(
            chat_id,
            "\n".join(
                [
                    "<b>Your workspace</b>",
                    f"tenant_id: <code>{h(tenant_id)}</code>",
                    f"bots: <code>{bot_count}</code>",
                    f"channels: <code>{channel_count}</code>",
                ]
            ),
            reply_id,
        )
        return

    if command == "/addbot":
        source_user = extract_identity_source(message)
        bot_id: int | None = None
        username: str | None = None
        label_parts: list[str] = []

        if source_user and source_user.get("is_bot"):
            bot_id = int(source_user["id"])
            username = source_user.get("username")

        for arg in args:
            if re.fullmatch(r"\d+", arg):
                bot_id = int(arg)
            elif arg.startswith("@"):
                username = arg
            else:
                label_parts.append(arg)

        if not bot_id:
            await send_message(
                chat_id,
                "I need a numeric bot user_id. Forward a message from the bot and reply "
                "with /addbot, or use /addbot 123456789 @BotUsername.",
                reply_id,
            )
            return

        storage.add_bot(tenant_id, bot_id, username, " ".join(label_parts) or None, owner_id)
        await send_message(
            chat_id,
            f"Registered {display_username(normalize_username(username))} "
            f"with ID <code>{bot_id}</code> in your workspace.",
            reply_id,
        )
        return

    if command == "/removebot":
        if not args:
            await send_message(chat_id, "Usage: /removebot @BotUsername or /removebot 123", reply_id)
            return
        removed = storage.remove_bot(tenant_id, args[0])
        await send_message(chat_id, f"Removed {removed} bot record(s).", reply_id)
        return

    if command == "/bots":
        await send_message(chat_id, render_bots(tenant_id), reply_id)
        return

    if command == "/addchannel":
        source_channel = extract_channel_source(message)
        target = args[0] if args else None
        title: str | None = None
        username: str | None = None
        added_chat_id: str | int | None = None

        if source_channel:
            added_chat_id = source_channel["id"]
            title = source_channel.get("title")
            username = source_channel.get("username")
        elif target:
            added_chat_id = target
            try:
                chat = await telegram.call("getChat", {"chat_id": telegram_chat_id(target)})
                added_chat_id = chat.get("id", target)
                title = chat.get("title")
                username = chat.get("username")
            except TelegramAPIError:
                pass

        if added_chat_id is None:
            await send_message(
                chat_id,
                "Usage: /addchannel @channel, or reply /addchannel to a forwarded channel post.",
                reply_id,
            )
            return
        storage.add_channel(tenant_id, added_chat_id, title, username, owner_id)
        await send_message(
            chat_id,
            f"Saved channel <code>{h(added_chat_id)}</code> in your workspace.",
            reply_id,
        )
        return

    if command == "/removechannel":
        if not args:
            await send_message(chat_id, "Usage: /removechannel @channel or /removechannel -100...", reply_id)
            return
        removed = storage.remove_channel(tenant_id, args[0])
        await send_message(chat_id, f"Removed {removed} channel record(s).", reply_id)
        return

    if command == "/channels":
        await send_message(chat_id, render_channels(tenant_id), reply_id)
        return

    if command == "/checkadmin":
        if not args:
            await send_message(chat_id, "Usage: /checkadmin @channel", reply_id)
            return
        if BOT_ID is None:
            await send_message(chat_id, "Bot is still starting. Try again.", reply_id)
            return
        try:
            member = await telegram.call(
                "getChatMember",
                {"chat_id": telegram_chat_id(args[0]), "user_id": BOT_ID},
            )
            lines = [
                f"<b>My status in {h(args[0])}</b>",
                f"status: <code>{h(member.get('status'))}</code>",
                f"can_promote_members: <code>{h(member.get('can_promote_members'))}</code>",
                f"can_manage_chat: <code>{h(member.get('can_manage_chat'))}</code>",
            ]
            await send_message(chat_id, "\n".join(lines), reply_id)
        except TelegramAPIError as exc:
            await send_message(chat_id, f"Check failed: {h(exc.description)}", reply_id)
        return

    if command == "/sync":
        targets = [normalize_chat_id(args[0])] if args else [
            c["chat_id"] for c in storage.list_channels(tenant_id)
        ]
        if not targets:
            await send_message(chat_id, "No channel target. Use /sync @channel.", reply_id)
            return
        for target in targets:
            lines = await sync_channel(tenant_id, target)
            await send_message(chat_id, "\n".join(lines), reply_id)
        return

    if command == "/syncall":
        channels = storage.list_channels(tenant_id)
        if not channels:
            await send_message(chat_id, "No saved channels. Use /addchannel first.", reply_id)
            return
        for channel in channels:
            lines = await sync_channel(tenant_id, channel["chat_id"])
            await send_message(chat_id, "\n".join(lines), reply_id)
        return

    if command == "/demote":
        if len(args) < 2:
            await send_message(chat_id, "Usage: /demote @channel @BotUsername", reply_id)
            return
        target_bot = storage.find_bot(tenant_id, args[1])
        if not target_bot:
            await send_message(chat_id, "That bot is not registered in your workspace.", reply_id)
            return
        ok, line = await demote_one(normalize_chat_id(args[0]), target_bot)
        await send_message(chat_id, ("[OK] " if ok else "[FAIL] ") + h(line), reply_id)
        return

    if command == "/admins":
        if not args:
            await send_message(chat_id, "Usage: /admins @channel", reply_id)
            return
        payload = {"chat_id": telegram_chat_id(args[0]), "return_bots": True}
        try:
            admins = await telegram.call("getChatAdministrators", payload)
        except TelegramAPIError:
            admins = await telegram.call(
                "getChatAdministrators", {"chat_id": telegram_chat_id(args[0])}
            )
        lines = [f"<b>Bot admins in {h(args[0])}</b>"]
        for admin in admins:
            user = admin.get("user") or {}
            if user.get("is_bot"):
                lines.append(
                    f"{display_username(normalize_username(user.get('username')))} "
                    f"| <code>{user.get('id')}</code> | {h(admin.get('status'))}"
                )
        if len(lines) == 1:
            lines.append("No bot admins visible.")
        await send_message(chat_id, "\n".join(lines), reply_id)
        return

    if command == "/export":
        payload = {
            "tenant_id": tenant_id,
            "bots": [
                {
                    "bot_user_id": bot["bot_user_id"],
                    "username": bot.get("username"),
                    "label": bot.get("label"),
                }
                for bot in storage.list_bots(tenant_id)
            ],
            "channels": [
                {
                    "chat_id": channel["chat_id"],
                    "title": channel.get("title"),
                    "username": channel.get("username"),
                }
                for channel in storage.list_channels(tenant_id)
            ],
        }
        import json

        await send_message(chat_id, f"<pre>{h(json.dumps(payload, indent=2))}</pre>", reply_id)
        return

    await send_message(chat_id, "Unknown command. Use /help.", reply_id)


async def handle_message(message: dict[str, Any]) -> None:
    chat_id = message["chat"]["id"]
    text = message.get("text") or message.get("caption") or ""
    command = ""
    args: list[str] = []
    if text.startswith("/"):
        command, args = command_parts(text)

    if command in {"/whoami", "/id"}:
        from_user = message.get("from") or {}
        tenant = active_tenant_for_user(from_user)
        lines = [
            f"chat_id: <code>{h(chat_id)}</code>",
            f"user_id: <code>{h(from_user.get('id'))}</code>",
            f"tenant_id: <code>{h(tenant.get('_id'))}</code>" if tenant else "tenant_id: not active",
        ]
        forwarded_user = extract_identity_source(message)
        forwarded_channel = extract_channel_source(message)
        if forwarded_user:
            lines.append(
                "forwarded_user: "
                f"<code>{h(forwarded_user.get('id'))}</code> "
                f"{display_username(normalize_username(forwarded_user.get('username')))}"
            )
        if forwarded_channel:
            lines.append(
                "forwarded_channel: "
                f"<code>{h(forwarded_channel.get('id'))}</code> "
                f"{display_username(normalize_username(forwarded_channel.get('username')))}"
            )
        await send_message(chat_id, "\n".join(lines), message.get("message_id"))
        return

    if command:
        await handle_admin_command(message, command, args)
        return

    from_user = message.get("from") or {}
    tenant = active_tenant_for_user(from_user)
    forwarded_user = extract_forwarded_user(message)
    if tenant and forwarded_user and forwarded_user.get("is_bot"):
        storage.add_bot(
            str(tenant["_id"]),
            int(forwarded_user["id"]),
            forwarded_user.get("username"),
            forwarded_user.get("first_name"),
            int(from_user["id"]),
        )
        await send_message(
            chat_id,
            f"Registered {display_username(normalize_username(forwarded_user.get('username')))} "
            f"with ID <code>{forwarded_user['id']}</code> in your workspace.",
            message.get("message_id"),
        )


async def handle_my_chat_member(update: dict[str, Any]) -> None:
    chat = update.get("chat") or {}
    actor = update.get("from") or {}
    tenant = active_tenant_for_user(actor)
    new_member = update.get("new_chat_member") or {}
    status = new_member.get("status")
    chat_type = chat.get("type")
    if chat_type not in {"channel", "supergroup"}:
        return

    if not tenant:
        if actor.get("id"):
            try:
                await send_message(
                    actor["id"],
                    "I was added to a channel, but your workspace is not active. "
                    "Send /invite CODE first.",
                )
            except TelegramAPIError:
                pass
        return

    tenant_id = str(tenant["_id"])
    if status in {"administrator", "creator"}:
        storage.add_channel(
            tenant_id,
            chat.get("id"),
            title=chat.get("title"),
            username=chat.get("username"),
            added_by=actor.get("id"),
        )
        msg = (
            f"Saved {h(chat_type)} <b>{h(chat.get('title', chat.get('id')))}</b> "
            f"(<code>{h(chat.get('id'))}</code>) in your workspace."
        )
        bots = storage.list_bots(tenant_id)
        if bots:
            synced = await sync_channel(tenant_id, str(chat.get("id")))
            msg += "\n\n" + "\n".join(synced)
        try:
            await send_message(int(tenant_id), msg)
        except TelegramAPIError:
            pass
    elif status in {"left", "kicked"}:
        storage.remove_channel(tenant_id, str(chat.get("id")))
        try:
            await send_message(
                int(tenant_id),
                f"Removed {h(chat_type)} <b>{h(chat.get('title', chat.get('id')))}</b> "
                "from your workspace because I am no longer a member.",
            )
        except TelegramAPIError:
            pass


async def handle_update(update: dict[str, Any]) -> None:
    if "message" in update:
        await handle_message(update["message"])
    elif "channel_post" in update:
        await handle_message(update["channel_post"])
    elif "my_chat_member" in update:
        await handle_my_chat_member(update["my_chat_member"])


async def initialize_telegram(set_webhook: bool = True) -> None:
    global BOT_ID, BOT_USERNAME
    storage.init()

    me = await telegram.call("getMe")
    BOT_ID = int(me["id"])
    BOT_USERNAME = me.get("username")

    await telegram.call(
        "setMyCommands",
        {
            "commands": [
                {"command": "help", "description": "Show commands"},
                {"command": "invite", "description": "Activate a shared workspace"},
                {"command": "workspace", "description": "Show workspace details"},
                {"command": "whoami", "description": "Show chat and user IDs"},
                {"command": "addbot", "description": "Register a file bot"},
                {"command": "bots", "description": "List registered bots"},
                {"command": "channels", "description": "List saved channels"},
                {"command": "sync", "description": "Promote registered bots"},
            ]
        },
    )

    rights = {"can_manage_chat": True, "can_invite_users": True, "can_promote_members": True}
    for_channels_payload = {"rights": rights, "for_channels": True}
    try:
        await telegram.call("setMyDefaultAdministratorRights", for_channels_payload)
    except TelegramAPIError:
        pass

    if set_webhook and settings.public_url:
        webhook_url = f"{settings.public_url}/telegram/webhook/{settings.webhook_secret}"
        await telegram.call(
            "setWebhook",
            {
                "url": webhook_url,
                "allowed_updates": settings.allowed_updates,
                "secret_token": settings.webhook_secret,
                "drop_pending_updates": False,
            },
        )


@app.on_event("startup")
async def startup() -> None:
    await initialize_telegram(set_webhook=True)


@app.on_event("shutdown")
async def shutdown() -> None:
    await telegram.close()
    storage.close()


@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return "Telegram Bot Administrator is running.\n"


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "bot_id": BOT_ID,
        "bot_username": BOT_USERNAME,
        "database": settings.mongodb_db,
    }


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if secret != settings.webhook_secret:
        raise HTTPException(status_code=404, detail="Not found")
    if x_telegram_bot_api_secret_token and not secrets.compare_digest(
        x_telegram_bot_api_secret_token, settings.webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Bad Telegram secret")

    update = await request.json()
    try:
        await handle_update(update)
    except Exception as exc:
        await notify_super_owners(f"Update handling error: <code>{h(exc)}</code>")
    return {"ok": True}


async def run_polling() -> None:
    await initialize_telegram(set_webhook=False)
    await telegram.call("deleteWebhook", {"drop_pending_updates": False})

    offset: int | None = None
    print("Polling started. Press Ctrl+C to stop.")
    while True:
        try:
            updates = await telegram.call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": settings.allowed_updates,
                },
            )
            for update in updates:
                offset = int(update["update_id"]) + 1
                await handle_update(update)
        except TelegramAPIError as exc:
            wait_for = exc.retry_after or 5
            print(f"Telegram error: {exc.description}; sleeping {wait_for}s")
            await asyncio.sleep(wait_for)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Polling error: {exc}; sleeping 5s")
            await asyncio.sleep(5)

    await telegram.close()
    storage.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "poll":
        asyncio.run(run_polling())
    else:
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
