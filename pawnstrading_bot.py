#!/usr/bin/env python3
"""PAWNS Telegram bot backend.

Single-file implementation using python-telegram-bot and MongoDB.

Install:
    pip install "python-telegram-bot[webhooks]==22.8" "pymongo>=4.11,<5" dnspython

Minimum environment variables:
    BOT_TOKEN=123456:telegram-bot-token
    MONGODB_URI=mongodb+srv://...
    ADMIN_CHAT_IDS=123456789[,987654321]

Run locally with long polling:
    RUN_MODE=polling python PAWNS_bot.py

Run behind a public HTTPS webhook:
    RUN_MODE=webhook
    WEBHOOK_BASE_URL=https://bot.example.com
    WEBHOOK_PATH=telegram
    WEBHOOK_SECRET=a-long-random-secret
    PORT=8080
    python PAWNS_bot.py

The bot never confirms payment automatically. A registration is stored with an
"Under Review" payment status and sent to the configured administrator DMs.
Only an authorised administrator can verify or reject it.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urlparse

from pymongo import ASCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, PyMongoError, ServerSelectionTimeoutError
from pymongo.server_api import ServerApi
from telegram import (
    BotCommand,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from chain_verifier import (
    get_explorer_url,
    normalize_txid,
    validate_txid_format,
    validate_wallet_format,
    verify_on_chain,
)

warnings.filterwarnings("ignore", category=PTBUserWarning)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
LOGGER = logging.getLogger("PAWNS")


# Conversation states
(
    FULL_NAME,
    INVESTMENT_AMOUNT,
    RISK_CATEGORY,
    DURATION,
    CONSENT,
    PAYMENT_DETAILS,
    AWAIT_TXID,
    ONBOARDING_INPUT,
) = range(8)


SERVICE_NAMES = {
    "private": "PAWNS Private Investment",
    "crypto": "Crypto Futures Trading",
    "forex_live": "Forex Live Account Trading",
    "forex_prop": "Forex Prop Firm Trading",
    "synthetic": "Synthetic Trading",
}

SERVICE_FEES = {
    "crypto": "$50 PAWNS service fee",
    "forex_live": "$50 PAWNS service fee",
    "forex_prop": "$50 PAWNS service fee (separate from any prop-firm challenge fee)",
    "synthetic": "$20 PAWNS service fee",
}

INVESTMENT_PLANS = {
    "high": {"2m": "50%", "3m": "100%", "6m": "200%", "12m": "400%"},
    "low": {"2m": "20%", "3m": "50%", "6m": "100%", "12m": "200%"},
}

DURATION_LABELS = {
    "2m": "2 months",
    "3m": "3 months",
    "6m": "6 months",
    "12m": "1 year",
}

LINK_SETTING_KEYS = {
    "bingx": "BINGX_URL",
    "broker": "BROKER_URL",
    "prop": "PROP_FIRM_URL",
    "synthetic": "SYNTHETIC_URL",
    "support": "SUPPORT_URL",
    "terms": "TERMS_URL",
    "investment_terms": "INVESTMENT_TERMS_URL",
}

FORBIDDEN_SECRET_RE = re.compile(
    r"\b(seed\s*phrase|private\s*key|wallet\s*key|password|passcode|one[- ]?time\s*(?:password|code)|otp|2fa\s*code)\b",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_admin_ids(raw: str) -> tuple[int, ...]:
    ids: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError as exc:
            raise RuntimeError(f"Invalid ADMIN_CHAT_IDS value: {item!r}") from exc
    if not ids:
        raise RuntimeError("ADMIN_CHAT_IDS must contain at least one Telegram chat ID")
    return tuple(dict.fromkeys(ids))


def is_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def clip(value: str, length: int = 500) -> str:
    return value.strip()[:length]


def make_reference(prefix: str = "BM") -> str:
    date_part = utc_now().strftime("%Y%m%d")
    return f"{prefix}-{date_part}-{secrets.token_hex(4).upper()}"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    mongodb_uri: str
    database_name: str
    admin_chat_ids: tuple[int, ...]
    referral_secret: str
    run_mode: str
    webhook_base_url: str
    webhook_path: str
    webhook_secret: str
    port: int
    investment_wallet: str
    trading_wallet: str
    investment_network: str
    trading_network: str
    fee_crypto: Decimal
    fee_forex_live: Decimal
    fee_forex_prop: Decimal
    fee_synthetic: Decimal
    payment_instructions_investment: str
    payment_instructions_trading: str
    about_text: str
    terms_text: str
    return_basis_text: str
    private_investment_enabled: bool
    minimum_investment: Decimal
    commission_percent: Decimal

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        mongo_uri = os.getenv("MONGODB_URI", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required")
        if mongo_uri.lower() in {"none", "memory", "mock", "false", "off"}:
            mongo_uri = ""

        try:
            minimum = Decimal(os.getenv("MINIMUM_INVESTMENT", "500"))
            commission = Decimal(os.getenv("REFERRAL_COMMISSION_PERCENT", "10"))
        except InvalidOperation as exc:
            raise RuntimeError("MINIMUM_INVESTMENT and REFERRAL_COMMISSION_PERCENT must be numbers") from exc

        investment_wallet = os.getenv("INVESTMENT_WALLET", "TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs").strip()
        trading_wallet = os.getenv("TRADING_WALLET", "0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67").strip()
        investment_network = os.getenv("INVESTMENT_NETWORK", "TRC20 / TRON").strip()
        trading_network = os.getenv("TRADING_NETWORK", "BSC / BEP20").strip()

        try:
            fee_crypto = Decimal(os.getenv("CRYPTO_FEE", "50"))
            fee_forex_live = Decimal(os.getenv("FOREX_LIVE_FEE", "50"))
            fee_forex_prop = Decimal(os.getenv("FOREX_PROP_FEE", "50"))
            fee_synthetic = Decimal(os.getenv("SYNTHETIC_FEE", "20"))
        except InvalidOperation as exc:
            raise RuntimeError("Service fees (CRYPTO_FEE, FOREX_LIVE_FEE, etc.) must be numbers") from exc

        instructions_invest = os.getenv(
            "PAYMENT_INSTRUCTIONS_INVESTMENT",
            "Send exact USDT amount via TRC20 / TRON network only.",
        ).strip()
        instructions_trade = os.getenv(
            "PAYMENT_INSTRUCTIONS_TRADING",
            "Send exact USD / USDT equivalent via BSC / BEP20 network only.",
        ).strip()

        mode = os.getenv("RUN_MODE", "polling").strip().lower()
        if mode not in {"polling", "webhook"}:
            raise RuntimeError("RUN_MODE must be 'polling' or 'webhook'")

        base_url = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
        path = os.getenv("WEBHOOK_PATH", "telegram").strip().strip("/") or "telegram"
        secret = os.getenv("WEBHOOK_SECRET", "").strip()
        if mode == "webhook":
            if not is_http_url(base_url) or not base_url.startswith("https://"):
                raise RuntimeError("WEBHOOK_BASE_URL must be a public HTTPS URL in webhook mode")
            if not secret:
                raise RuntimeError("WEBHOOK_SECRET is required in webhook mode")

        referral_secret = os.getenv("REFERRAL_SECRET", "").strip()
        if not referral_secret:
            # Stable referral IDs require this value to remain unchanged in production.
            referral_secret = hashlib.sha256(token.encode("utf-8")).hexdigest()

        return cls(
            bot_token=token,
            mongodb_uri=mongo_uri,
            database_name=os.getenv("MONGODB_DATABASE", "PAWNS").strip() or "PAWNS",
            admin_chat_ids=parse_admin_ids(os.getenv("ADMIN_CHAT_IDS", "")),
            referral_secret=referral_secret,
            run_mode=mode,
            webhook_base_url=base_url,
            webhook_path=path,
            webhook_secret=secret,
            port=int(os.getenv("PORT", "8080")),
            investment_wallet=investment_wallet,
            trading_wallet=trading_wallet,
            investment_network=investment_network,
            trading_network=trading_network,
            fee_crypto=fee_crypto,
            fee_forex_live=fee_forex_live,
            fee_forex_prop=fee_forex_prop,
            fee_synthetic=fee_synthetic,
            payment_instructions_investment=instructions_invest,
            payment_instructions_trading=instructions_trade,
            about_text=os.getenv(
                "ABOUT_TEXT",
                "PAWNS provides investment, trading onboarding, and related financial services.",
            ).strip(),
            terms_text=os.getenv(
                "TERMS_TEXT",
                "Trading and investment involve risk. Returns are not guaranteed and capital may be lost. "
                "Only proceed after reading the applicable agreement and risk disclosure.",
            ).strip(),
            return_basis_text=os.getenv(
                "RETURN_BASIS_TEXT",
                "The signed investment agreement defines whether a stated figure is gross profit, net profit, "
                "or total payout, together with fees, loss conditions, payment timing, and treatment of principal.",
            ).strip(),
            private_investment_enabled=env_bool("PRIVATE_INVESTMENT_ENABLED", False),
            minimum_investment=minimum,
            commission_percent=commission,
        )


class MockUpdateResult:
    def __init__(self, modified_count: int = 1) -> None:
        self.modified_count = modified_count
        self.acknowledged = True


class AsyncCursorWrapper:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self._iter: Any = None

    def sort(self, key_or_list: Any, direction: int = 1) -> "AsyncCursorWrapper":
        if isinstance(key_or_list, list) and key_or_list:
            key, direction = key_or_list[0]
        else:
            key = key_or_list
        desc = direction == -1
        self._items.sort(key=lambda x: str(x.get(key, "")), reverse=desc)
        return self

    def limit(self, count: int) -> "AsyncCursorWrapper":
        self._items = self._items[:count]
        return self

    def __aiter__(self) -> "AsyncCursorWrapper":
        self._iter = iter(self._items)
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._iter is None:
            self._iter = iter(self._items)
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        return copy.deepcopy(self._items[:length] if length is not None else self._items)



def _matches(doc: dict[str, Any], filter_dict: dict[str, Any]) -> bool:
    for k, v in filter_dict.items():
        if k == "$or":
            if not any(_matches(doc, cond) for cond in v):
                return False
            continue
        doc_val = doc.get(k)
        if isinstance(v, dict):
            if "$exists" in v:
                exists = k in doc and doc[k] is not None
                if exists != v["$exists"]:
                    return False
        else:
            if doc_val != v:
                return False
    return True


def _apply_update(doc: dict[str, Any], update_dict: dict[str, Any], is_insert: bool = False) -> None:
    if "$set" in update_dict:
        doc.update(copy.deepcopy(update_dict["$set"]))
    if is_insert and "$setOnInsert" in update_dict:
        for k, v in update_dict["$setOnInsert"].items():
            doc[k] = copy.deepcopy(v)


class InMemoryCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.docs: list[dict[str, Any]] = []

    async def create_index(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def insert_one(self, doc: dict[str, Any]) -> dict[str, Any]:
        doc_copy = copy.deepcopy(doc)
        if "_id" not in doc_copy:
            doc_copy["_id"] = str(uuid.uuid4())
        self.docs.append(doc_copy)
        return doc_copy

    async def find_one(self, filter_dict: dict[str, Any]) -> dict[str, Any] | None:
        for d in self.docs:
            if _matches(d, filter_dict):
                return copy.deepcopy(d)
        return None

    async def update_one(
        self, filter_dict: dict[str, Any], update_dict: dict[str, Any], upsert: bool = False
    ) -> MockUpdateResult:
        for d in self.docs:
            if _matches(d, filter_dict):
                _apply_update(d, update_dict, is_insert=False)
                return MockUpdateResult(1)
        if upsert:
            new_doc: dict[str, Any] = {}
            for k, v in filter_dict.items():
                if not k.startswith("$") and not isinstance(v, dict):
                    new_doc[k] = v
            _apply_update(new_doc, update_dict, is_insert=True)
            await self.insert_one(new_doc)
            return MockUpdateResult(1)
        return MockUpdateResult(0)

    async def find_one_and_update(
        self,
        filter_dict: dict[str, Any],
        update_dict: dict[str, Any],
        upsert: bool = False,
        return_document: Any = None,
    ) -> dict[str, Any] | None:
        for d in self.docs:
            if _matches(d, filter_dict):
                _apply_update(d, update_dict, is_insert=False)
                return copy.deepcopy(d)
        if upsert:
            new_doc = {}
            for k, v in filter_dict.items():
                if not k.startswith("$") and not isinstance(v, dict):
                    new_doc[k] = v
            _apply_update(new_doc, update_dict, is_insert=True)
            await self.insert_one(new_doc)
            return copy.deepcopy(new_doc)
        return None

    def find(self, filter_dict: dict[str, Any] | None = None) -> AsyncCursorWrapper:
        f = filter_dict or {}
        matched = [copy.deepcopy(d) for d in self.docs if _matches(d, f)]
        return AsyncCursorWrapper(matched)

    async def count_documents(self, filter_dict: dict[str, Any]) -> int:
        return sum(1 for d in self.docs if _matches(d, filter_dict))

    async def aggregate(self, pipeline: list[dict[str, Any]]) -> AsyncCursorWrapper:
        match_stage: dict[str, Any] = {}
        for stage in pipeline:
            if "$match" in stage:
                match_stage = stage["$match"]
        matched = [d for d in self.docs if _matches(d, match_stage)]
        grouped: dict[str, Decimal] = {}
        for d in matched:
            curr = d.get("currency", "USD")
            share = Decimal(str(d.get("referrer_share", "0")))
            grouped[curr] = grouped.get(curr, Decimal("0")) + share
        results = [{"_id": curr, "total": total} for curr, total in grouped.items()]
        return AsyncCursorWrapper(results)


class Database:
    def __init__(self, settings: Settings) -> None:
        self._bot_settings = settings
        self.client: AsyncMongoClient | None = None
        raw_uri = (settings.mongodb_uri or "").strip().strip("\"'")
        self.is_memory_mode = not bool(raw_uri)
        if not self.is_memory_mode:
            timeout_ms = int(os.getenv("MONGODB_TIMEOUT_MS", "5000"))
            client_kwargs: dict[str, Any] = {
                "retryWrites": True,
                "appname": "PAWNS-telegram-bot",
                "serverSelectionTimeoutMS": timeout_ms,
            }
            if raw_uri.startswith("mongodb+srv://") or os.getenv("MONGODB_SERVER_API", "").lower() in ("true", "1"):
                client_kwargs["server_api"] = ServerApi("1")

            self.client = AsyncMongoClient(raw_uri, **client_kwargs)
            try:
                default_db = self.client.get_default_database()
                self.db = default_db if default_db is not None else self.client[settings.database_name]
            except Exception:
                self.db = self.client[settings.database_name]
            self.users = self.db.users
            self.submissions = self.db.submissions
            self.settings = self.db.settings
            self.commissions = self.db.commission_events
            self.audit = self.db.audit_log
        else:
            self._init_memory_db()

    def _init_memory_db(self) -> None:
        self.is_memory_mode = True
        self.client = None
        self.users = InMemoryCollection("users")
        self.submissions = InMemoryCollection("submissions")
        self.settings = InMemoryCollection("settings")
        self.commissions = InMemoryCollection("commissions")
        self.audit = InMemoryCollection("audit")

    async def initialize(self) -> None:
        if self.is_memory_mode:
            LOGGER.warning("Running with IN-MEMORY storage (no MongoDB). Data will NOT persist across restarts.")
            return

        try:
            await self.client.admin.command({"ping": 1})
            await self.users.create_index("telegram_id", unique=True)
            await self.users.create_index("referral_id", unique=True)
            await self.users.create_index("referred_by")
            await self.submissions.create_index("reference", unique=True)
            await self.submissions.create_index([("telegram_id", ASCENDING), ("created_at", ASCENDING)])
            await self.submissions.create_index("payment_status")
            await self.submissions.create_index("txid")
            await self.commissions.create_index([("referrer_telegram_id", ASCENDING), ("status", ASCENDING)])
            await self.audit.create_index("created_at")
            LOGGER.info("Connected to MongoDB successfully; database indexes ready")
        except (ServerSelectionTimeoutError, ConnectionFailure, PyMongoError, OSError) as exc:
            LOGGER.warning(
                "Could not connect to MongoDB at '%s' (%s). "
                "Falling back to IN-MEMORY storage for testing (data will NOT persist across restarts).",
                self._bot_settings.mongodb_uri,
                exc,
            )
            self._init_memory_db()

    async def close(self) -> None:
        if self.client:
            await self.client.close()

    async def upsert_user(self, telegram_user: Any, referral_id: str) -> dict[str, Any]:
        now = utc_now()
        return await self.users.find_one_and_update(
            {"telegram_id": telegram_user.id},
            {
                "$set": {
                    "username": telegram_user.username,
                    "telegram_display_name": telegram_user.full_name,
                    "last_seen_at": now,
                },
                "$setOnInsert": {
                    "telegram_id": telegram_user.id,
                    "referral_id": referral_id,
                    "registered_at": now,
                    "referral_earnings": "0",
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def attribute_referral(self, telegram_id: int, referral_code: str) -> bool:
        referrer = await self.users.find_one({"referral_id": referral_code})
        if not referrer or referrer["telegram_id"] == telegram_id:
            return False
        result = await self.users.update_one(
            {
                "telegram_id": telegram_id,
                "$or": [
                    {"referred_by": {"$exists": False}},
                    {"referred_by": None},
                ],
            },
            {
                "$set": {
                    "referred_by": referrer["telegram_id"],
                    "referral_at": utc_now(),
                }
            },
        )
        return result.modified_count == 1

    async def get_setting(self, key: str, fallback: str = "") -> str:
        doc = await self.settings.find_one({"key": key})
        if doc:
            return str(doc.get("value", ""))
        return fallback

    async def set_setting(self, key: str, value: str, admin_id: int) -> dict[str, Any]:
        now = utc_now()
        existing = await self.settings.find_one({"key": key})
        old_val = str(existing.get("value", "")) if existing else ""
        await self.settings.update_one(
            {"key": key},
            {"$set": {"value": value, "updated_at": now, "updated_by": admin_id}},
            upsert=True,
        )
        audit_entry = {
            "action": "setting_updated",
            "setting": key,
            "old_value": old_val,
            "new_value": value,
            "admin_id": admin_id,
            "created_at": now,
        }
        await self.audit.insert_one(audit_entry)
        return audit_entry


async def get_service_payment_info(
    context: ContextTypes.DEFAULT_TYPE,
    service: str,
    investment_amount: str | None = None,
) -> dict[str, Any]:
    db = get_db(context)
    settings = get_settings(context)
    if service == "private":
        wallet = await db.get_setting("investment_wallet", settings.investment_wallet)
        network = await db.get_setting("investment_network", settings.investment_network)
        instructions = await db.get_setting(
            "payment_instructions_investment",
            settings.payment_instructions_investment,
        )
        currency = "USDT"
        amount = investment_amount or str(settings.minimum_investment)
    else:
        wallet = await db.get_setting("trading_wallet", settings.trading_wallet)
        network = await db.get_setting("trading_network", settings.trading_network)
        instructions = await db.get_setting(
            "payment_instructions_trading",
            settings.payment_instructions_trading,
        )
        currency = "USD / USDT"
        fee_map = {
            "crypto": ("fee_crypto", settings.fee_crypto),
            "forex_live": ("fee_forex_live", settings.fee_forex_live),
            "forex_prop": ("fee_forex_prop", settings.fee_forex_prop),
            "synthetic": ("fee_synthetic", settings.fee_synthetic),
        }
        key, default_fee = fee_map[service]
        amount = await db.get_setting(key, str(default_fee))

    return {
        "service": service,
        "service_name": SERVICE_NAMES.get(service, service),
        "amount": amount,
        "currency": currency,
        "network": network,
        "wallet": wallet,
        "instructions": instructions,
    }


def render_payment_screen(payment_info: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "💳 <b>PAYMENT DETAILS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Service:</b> {html.escape(payment_info['service_name'])}\n"
        f"<b>Amount:</b> ${html.escape(str(payment_info['amount']))}\n"
        f"<b>Payment Currency:</b> {html.escape(payment_info['currency'])}\n"
        f"<b>Required Network:</b> {html.escape(payment_info['network'])}\n\n"
        "<b>PAWNS Payment Address:</b>\n"
        f"<code>{html.escape(payment_info['wallet'])}</code>\n\n"
        f"<b>Instructions:</b>\n{html.escape(payment_info['instructions'])}\n\n"
        f"⚠️ <b>IMPORTANT:</b> Send the payment only through the specified network ({html.escape(payment_info['network'])}). "
        "Sending funds through another network may result in loss of funds."
    )
    buttons = [
        [InlineKeyboardButton("💳 I've Made Payment", callback_data="pay:confirm")],
        [InlineKeyboardButton("📋 Copy Wallet Address", copy_text=CopyTextButton(text=payment_info["wallet"]))],
        [InlineKeyboardButton("❌ Cancel", callback_data="pay:cancel")],
    ]
    return text, InlineKeyboardMarkup(buttons)



def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.admin_chat_ids


def referral_id_for(telegram_id: int, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), str(telegram_id).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"BIT{digest[:10].upper()}"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("♟️ Private Investment", callback_data="service:private")],
            [InlineKeyboardButton("📈 Crypto Futures", callback_data="service:crypto")],
            [InlineKeyboardButton("💱 Forex Trading", callback_data="service:forex")],
            [InlineKeyboardButton("📊 Synthetic Trading", callback_data="service:synthetic")],
            [InlineKeyboardButton("🤝 Referral Program", callback_data="referral")],
            [
                InlineKeyboardButton("ℹ️ About", callback_data="about"),
                InlineKeyboardButton("🛟 Support", callback_data="support"),
            ],
            [InlineKeyboardButton("📄 Terms & Risk Disclosure", callback_data="terms")],
        ]
    )


def back_keyboard(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=target)]])


async def send_or_edit(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            LOGGER.debug("Could not edit menu message; sending a new one: %s", exc)
    if update.effective_message:
        await update.effective_message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    user = update.effective_user
    if not user:
        return None
    settings = get_settings(context)
    return await get_db(context).upsert_user(
        user,
        referral_id_for(user.id, settings.referral_secret),
    )


def main_menu_text(notice: str = "") -> str:
    prefix = f"⚠️ <b>{html.escape(notice)}</b>\n\n" if notice else ""
    return (
        f"{prefix}♟️ <b>PAWNS BOT — TRADING &amp; ONBOARDING</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "Welcome to <b>PAWNS</b>.\n\n"
        "Explore our automated trading and investment onboarding services through the menu below.\n\n"
        "⬇️ <b>Select an option to continue:</b> ⬇️"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_doc = await ensure_user(update, context)
    if not user_doc:
        return

    if context.args:
        payload = context.args[0].strip()
        if payload.startswith("ref_"):
            await get_db(context).attribute_referral(update.effective_user.id, payload[4:])

    await send_or_edit(update, main_menu_text(), main_menu_keyboard())


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update, context)
    context.user_data.pop("registration", None)
    await send_or_edit(
        update,
        main_menu_text(),
        main_menu_keyboard(),
    )


def configurable_link_button(label: str, url: str, missing_key: str) -> InlineKeyboardButton:
    if is_http_url(url):
        return InlineKeyboardButton(label, url=url)
    return InlineKeyboardButton(f"{label} (not configured)", callback_data=f"not_configured:{missing_key}")


async def get_link(context: ContextTypes.DEFAULT_TYPE, short_key: str) -> str:
    env_name = LINK_SETTING_KEYS[short_key]
    return await get_db(context).get_setting(short_key, os.getenv(env_name, "").strip())


async def show_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    terms_url = await get_link(context, "investment_terms")
    start_label = "💰 Invest Now" if settings.private_investment_enabled else "🔒 Registration not yet enabled"
    start_callback = "register:private" if settings.private_investment_enabled else "not_configured:private_investment"
    buttons = [
        [InlineKeyboardButton(start_label, callback_data=start_callback)],
        [InlineKeyboardButton("📊 View Investment Plans", callback_data="service:plans")],
        [configurable_link_button("📄 Investment Terms", terms_url, "investment_terms")],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="menu")],
    ]
    text = (
        "♟️ <b>PAWNS PRIVATE INVESTMENT</b>\n\n"
        "Explore the available proposed investment plans, choose a duration, and review the applicable "
        "agreement and risk information before proceeding.\n\n"
        f"<b>Minimum Investment:</b> ${settings.minimum_investment}\n\n"
        "⚠️ Investment involves risk. Returns are not guaranteed, and invested capital may be lost."
    )
    if not settings.private_investment_enabled:
        text += (
            "\n\n<i>Registration is disabled until the operator explicitly enables it after configuring "
            "the final agreement, return basis, and payment instructions.</i>"
        )
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    rows = []
    for risk, plans in INVESTMENT_PLANS.items():
        label = "Higher-risk" if risk == "high" else "Lower-risk"
        rows.append(f"<b>{label} proposed terms</b>")
        rows.extend(f"• {DURATION_LABELS[key]} — {value}" for key, value in plans.items())
        rows.append("")
    rows.append("These figures are proposed return objectives, not assured outcomes.")
    rows.append(html.escape(settings.return_basis_text))
    await send_or_edit(update, "\n".join(rows), back_keyboard("service:private"))


async def show_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bingx_url = await get_link(context, "bingx")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Pay $50 / Start Registration", callback_data="register:crypto")],
            [configurable_link_button("🔗 BingX Registration", bingx_url, "bingx")],
            [InlineKeyboardButton("📋 Already Have a BingX UID?", callback_data="register:crypto")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
        ]
    )
    await send_or_edit(
        update,
        "📈 <b>PAWNS CRYPTO FUTURES TRADING</b>\n\n"
        "Access PAWNS crypto-futures onboarding.\n\n"
        "<b>Service Fee:</b> $50\n"
        "<b>Requirement:</b> BingX UID\n\n"
        "Payment is confirmed only after administrator verification.",
        keyboard,
    )


async def show_forex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit(
        update,
        "💱 <b>CHOOSE FOREX TRADING TYPE</b>\n\n"
        "The $50 PAWNS service fee is separate from broker deposits or prop-firm challenge fees.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Live Account Trading", callback_data="service:forex_live")],
                [InlineKeyboardButton("Prop Firm Trading", callback_data="service:forex_prop")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
            ]
        ),
    )


async def show_forex_live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = await get_link(context, "broker")
    await send_or_edit(
        update,
        "💱 <b>FOREX LIVE ACCOUNT</b>\n\n"
        "Access PAWNS live-account onboarding.\n\n"
        "<b>Service Fee:</b> $50\n<b>Requirement:</b> Broker account\n\n"
        "Never send a password, private key, seed phrase, or authentication code.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Pay $50 / Submit Details", callback_data="register:forex_live")],
                [configurable_link_button("🔗 Broker Registration", url, "broker")],
                [InlineKeyboardButton("⬅️ Back", callback_data="service:forex")],
            ]
        ),
    )


async def show_forex_prop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = await get_link(context, "prop")
    await send_or_edit(
        update,
        "🏆 <b>FOREX PROP FIRM</b>\n\n"
        "Access PAWNS prop-firm onboarding.\n\n"
        "<b>PAWNS Service Fee:</b> $50\n"
        "Any prop-firm challenge fee is separate and payable under that provider's terms.\n\n"
        "Never send a password, private key, seed phrase, or authentication code.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Pay $50 / Submit Details", callback_data="register:forex_prop")],
                [configurable_link_button("🔗 Prop Firm Registration", url, "prop")],
                [InlineKeyboardButton("⬅️ Back", callback_data="service:forex")],
            ]
        ),
    )


async def show_synthetic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = await get_link(context, "synthetic")
    await send_or_edit(
        update,
        "📊 <b>PAWNS SYNTHETIC TRADING</b>\n\n"
        "Access PAWNS synthetic-trading onboarding.\n\n"
        "<b>Service Fee:</b> $20\n\n"
        "The configured provider and exact service deliverable should be reviewed before payment.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Pay $20 / Start Registration", callback_data="register:synthetic")],
                [configurable_link_button("🔗 Synthetic Trading Link", url, "synthetic")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
            ]
        ),
    )


async def show_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit(update, f"<b>ABOUT PAWNS</b>\n\n{html.escape(get_settings(context).about_text)}", back_keyboard())


async def show_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    terms_url = await get_link(context, "terms")
    buttons = []
    if is_http_url(terms_url):
        buttons.append([InlineKeyboardButton("Open full terms", url=terms_url)])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu")])
    settings = get_settings(context)
    await send_or_edit(
        update,
        "<b>TERMS & RISK DISCLOSURE</b>\n\n"
        f"{html.escape(settings.terms_text)}\n\n"
        "The bot does not provide personalised financial advice and does not automatically confirm payments.",
        InlineKeyboardMarkup(buttons),
    )


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    support_url = await get_link(context, "support")
    keyboard = InlineKeyboardMarkup(
        [
            [configurable_link_button("Contact PAWNS Support", support_url, "support")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
        ]
    )
    text = (
        "🛟 <b>PAWNS SUPPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Have questions about onboarding, verification, or our trading services?\n\n"
        "Use the button below to reach the PAWNS support team directly."
    )
    await send_or_edit(update, text, keyboard)


async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_doc = await ensure_user(update, context)
    if not user_doc:
        return
    db = get_db(context)
    user_id = update.effective_user.id
    bot_username = context.application.bot_data.get("bot_username") or context.bot.username
    referral_link = f"https://t.me/{bot_username}?start=ref_{user_doc['referral_id']}"
    referred_count = await db.users.count_documents({"referred_by": user_id})

    verified_pipeline = [
        {"$match": {"referrer_telegram_id": user_id, "status": "verified"}},
        {"$group": {"_id": "$currency", "total": {"$sum": {"$toDecimal": "$referrer_share"}}}},
    ]
    earnings: list[str] = []
    async for row in await db.commissions.aggregate(verified_pipeline):
        earnings.append(f"{row['_id']} {row['total']}")
    earnings_text = ", ".join(earnings) if earnings else "No verified earnings yet"

    settings = get_settings(context)
    await send_or_edit(
        update,
        "🤝 <b>PAWNS REFERRAL PROGRAM</b>\n\n"
        f"Earn <b>{settings.commission_percent}% of eligible affiliate commission</b> from qualifying referrals, "
        "subject to the referral terms. This is not a percentage of a customer's investment or trading volume.\n\n"
        f"<b>Your Referral ID:</b> <code>{html.escape(user_doc['referral_id'])}</code>\n"
        f"<b>Your Referral Link:</b> {html.escape(referral_link)}\n"
        f"<b>Attributed Referrals:</b> {referred_count}\n"
        f"<b>Verified Earnings:</b> {html.escape(earnings_text)}",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📄 Referral Terms", callback_data="terms")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
            ]
        ),
    )


async def not_configured(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer(
            "This item has not yet been configured. Please contact support.",
            show_alert=True,
        )


async def route_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = update.callback_query.data
    routes = {
        "menu": show_main_menu,
        "service:private": show_private,
        "service:plans": show_plans,
        "service:crypto": show_crypto,
        "service:forex": show_forex,
        "service:forex_live": show_forex_live,
        "service:forex_prop": show_forex_prop,
        "service:synthetic": show_synthetic,
        "about": show_about,
        "support": show_support,
        "terms": show_terms,
        "referral": show_referral,
    }
    handler = routes.get(data)
    if handler:
        await handler(update, context)


async def registration_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user(update, context)
    query = update.callback_query
    await query.answer()
    service = query.data.split(":", 1)[1]
    if service not in SERVICE_NAMES:
        await query.edit_message_text("This service is not available.")
        return ConversationHandler.END
    if service == "private" and not get_settings(context).private_investment_enabled:
        await query.edit_message_text(
            "Private-investment registration is not enabled yet. Please contact support.",
            reply_markup=back_keyboard("service:private"),
        )
        return ConversationHandler.END

    context.user_data["registration"] = {"service": service}
    await query.edit_message_text(
        f"<b>{html.escape(SERVICE_NAMES[service])}</b>\n\n"
        "Please enter your full legal name.\n\n"
        "Send /cancel at any time to stop this registration.",
        parse_mode=ParseMode.HTML,
    )
    return FULL_NAME


async def receive_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = clip(update.effective_message.text or "", 120)
    if len(name.split()) < 2 or any(char.isdigit() for char in name):
        await update.effective_message.reply_text("Please enter your full legal name using at least two words.")
        return FULL_NAME

    registration = context.user_data["registration"]
    registration["full_name"] = name
    service = registration["service"]

    if service == "private":
        db = get_db(context)
        settings = get_settings(context)
        min_invest = await db.get_setting("minimum_investment", str(settings.minimum_investment))
        await update.effective_message.reply_text(
            f"How much would you like to invest in USD?\n\nMinimum investment: ${min_invest}."
        )
        return INVESTMENT_AMOUNT

    # For trading services, display payment details directly
    payment_info = await get_service_payment_info(context, service)
    registration["payment_info"] = payment_info
    text, keyboard = render_payment_screen(payment_info)
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return PAYMENT_DETAILS


async def receive_investment_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.effective_message.text or "").strip().replace("$", "").replace(",", "")
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        await update.effective_message.reply_text("Enter a valid number, for example 500 or 1000.00.")
        return INVESTMENT_AMOUNT

    db = get_db(context)
    settings = get_settings(context)
    min_invest_str = await db.get_setting("minimum_investment", str(settings.minimum_investment))
    minimum = Decimal(min_invest_str)

    if amount < minimum:
        await update.effective_message.reply_text(f"The minimum investment is ${minimum}.")
        return INVESTMENT_AMOUNT
    if amount > Decimal("1000000000"):
        await update.effective_message.reply_text("That amount is outside the supported range. Please contact support.")
        return INVESTMENT_AMOUNT

    context.user_data["registration"]["investment_amount"] = str(amount)
    await update.effective_message.reply_text(
        f"Selected amount: <b>${amount}</b>\n\nChoose a risk category:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Higher Risk", callback_data="risk:high")],
                [InlineKeyboardButton("Lower Risk", callback_data="risk:low")],
            ]
        ),
    )
    return RISK_CATEGORY


async def select_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    risk = query.data.split(":", 1)[1]
    if risk not in INVESTMENT_PLANS:
        return RISK_CATEGORY
    context.user_data["registration"]["risk_category"] = risk
    buttons = [
        [InlineKeyboardButton(DURATION_LABELS[key], callback_data=f"duration:{key}")]
        for key in INVESTMENT_PLANS[risk]
    ]
    await query.edit_message_text(
        "Choose an investment duration. Proposed figures are return objectives, not guaranteed returns.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DURATION


async def select_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    duration = query.data.split(":", 1)[1]
    registration = context.user_data["registration"]
    risk = registration["risk_category"]
    if duration not in INVESTMENT_PLANS[risk]:
        return DURATION
    registration["duration"] = DURATION_LABELS[duration]
    registration["proposed_return"] = INVESTMENT_PLANS[risk][duration]
    return await ask_for_consent(update, context)


def registration_summary(registration: dict[str, Any]) -> str:
    service = registration["service"]
    lines = [
        f"<b>Service:</b> {html.escape(SERVICE_NAMES[service])}",
        f"<b>Full name:</b> {html.escape(registration['full_name'])}",
    ]
    if service == "private":
        lines.extend(
            [
                f"<b>Amount:</b> ${html.escape(registration.get('investment_amount', '0'))}",
                f"<b>Risk category:</b> {html.escape(registration.get('risk_category', '').title())}",
                f"<b>Duration:</b> {html.escape(registration.get('duration', ''))}",
                f"<b>Proposed return:</b> {html.escape(registration.get('proposed_return', ''))}",
            ]
        )
    return "\n".join(lines)


async def ask_for_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    registration = context.user_data["registration"]
    settings = get_settings(context)
    risk_text = html.escape(settings.terms_text)
    if registration["service"] == "private":
        risk_text += "\n\n" + html.escape(settings.return_basis_text)
    text = (
        "<b>Review your registration</b>\n\n"
        f"{registration_summary(registration)}\n\n"
        f"<b>Risk disclosure:</b> {risk_text}\n\n"
        "By selecting ‘I agree and continue’, you confirm that the details are accurate, you have read the "
        "applicable terms and risk disclosure, and you consent to PAWNS storing and reviewing this "
        "information for onboarding."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ I agree and continue", callback_data="consent:yes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="consent:no")],
        ]
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return CONSENT


async def receive_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    decision = query.data.split(":", 1)[1]
    if decision != "yes":
        context.user_data.pop("registration", None)
        await query.edit_message_text(
            main_menu_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    registration = context.user_data["registration"]
    registration["consent_at"] = utc_now()
    payment_info = await get_service_payment_info(
        context,
        registration["service"],
        investment_amount=registration.get("investment_amount"),
    )
    registration["payment_info"] = payment_info
    text, keyboard = render_payment_screen(payment_info)
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return PAYMENT_DETAILS


async def receive_payment_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "pay:cancel":
        context.user_data.pop("registration", None)
        await query.edit_message_text(
            main_menu_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    if data == "pay:confirm":
        registration = context.user_data.get("registration", {})
        payment_info = registration.get("payment_info", {})
        network = payment_info.get("network", "the specified network")
        await query.edit_message_text(
            "<b>Please enter your transaction hash / TXID.</b>\n\n"
            f"Submit the exact transaction hash from your wallet or exchange for payment sent on the <b>{html.escape(network)}</b> network.\n\n"
            "⚠️ Do not send passwords, seed phrases, private keys, or authentication codes.\n"
            "Screenshots alone cannot be verified automatically.",
            parse_mode=ParseMode.HTML,
        )
        return AWAIT_TXID

    return PAYMENT_DETAILS


async def receive_txid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if not message.text:
        await message.reply_text(
            "⚠️ The verification desk requires your transaction hash / TXID as text. "
            "Screenshots or receipt files alone cannot be verified automatically.\n\n"
            "Please enter your transaction hash / TXID as text:"
        )
        return AWAIT_TXID

    raw_txid = message.text.strip()
    if FORBIDDEN_SECRET_RE.search(raw_txid):
        await message.reply_text(
            "Security Notice: Do not send passwords, seed phrases, private keys, or authentication codes. "
            "Please enter only your transaction hash / TXID."
        )
        return AWAIT_TXID

    registration = context.user_data.get("registration")
    if not registration or "payment_info" not in registration:
        await message.reply_text("Session expired. Please restart registration from /menu.")
        return ConversationHandler.END

    payment_info = registration["payment_info"]
    network = payment_info["network"]

    if not validate_txid_format(raw_txid, network):
        await message.reply_text(
            f"❌ <b>Invalid transaction hash format for {html.escape(network)}</b>.\n\n"
            "Please check your wallet/exchange and submit the exact transaction hash (TXID) without extra spaces or symbols.",
            parse_mode=ParseMode.HTML,
        )
        return AWAIT_TXID

    normalized = normalize_txid(raw_txid, network)
    db = get_db(context)

    # Replay protection check
    dup = await db.submissions.find_one(
        {"txid": normalized, "payment_status": {"$in": ["PENDING", "VERIFIED ✅", "Verified"]}}
    )
    if dup:
        await message.reply_text(
            "⚠️ This transaction hash has already been registered in our system. "
            "If you have already paid, please await administrator verification or contact support.",
            reply_markup=main_menu_keyboard(),
        )
        return AWAIT_TXID

    # Run on-chain verification
    chain_result = await verify_on_chain(
        txid=normalized,
        network=network,
        expected_wallet=payment_info["wallet"],
        expected_amount=Decimal(payment_info["amount"]),
    )

    user = update.effective_user
    reference = make_reference()
    now = utc_now()
    user_doc = await db.users.find_one({"telegram_id": user.id})

    submission = {
        "reference": reference,
        "telegram_id": user.id,
        "telegram_username": user.username,
        "full_name": registration.get("full_name", user.full_name),
        "service": payment_info["service"],
        "service_name": payment_info["service_name"],
        "amount": str(payment_info["amount"]),
        "currency": payment_info["currency"],
        "network": network,
        "wallet_address": payment_info["wallet"],
        "txid": normalized,
        "explorer_url": chain_result.explorer_url,
        "chain_verification": {
            "status": chain_result.status,
            "details": chain_result.details,
            "confirmations": chain_result.confirmations,
            "detected_to": chain_result.detected_to,
            "detected_amount": chain_result.detected_amount,
            "checked_at": now,
        },
        "selected_plan": {
            key: registration[key]
            for key in ("investment_amount", "risk_category", "duration", "proposed_return")
            if key in registration
        },
        "payment_status": "PENDING",
        "admin_verification_status": {
            "decision": "Pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "note": "",
        },
        "onboarding_status": "Pending",
        "created_at": now,
        "updated_at": now,
        "referred_by": user_doc.get("referred_by") if user_doc else None,
    }

    await db.submissions.insert_one(submission)
    await db.users.update_one(
        {"telegram_id": user.id},
        {
            "$set": {
                "full_name": registration["full_name"],
                "selected_service": payment_info["service"],
                "last_submission_reference": reference,
                "updated_at": now,
            }
        },
    )
    await db.audit.insert_one(
        {"action": "payment_submitted", "reference": reference, "telegram_id": user.id, "created_at": now}
    )

    user_reply = (
        "⏳ <b>Payment Status: PENDING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{reference}</code>\n"
        f"<b>Service:</b> {html.escape(payment_info['service_name'])}\n"
        f"<b>Amount:</b> ${html.escape(str(payment_info['amount']))} {html.escape(payment_info['currency'])}\n"
        f"<b>Network:</b> {html.escape(network)}\n"
        f"<b>PAWNS Wallet:</b> <code>{html.escape(payment_info['wallet'])}</code>\n"
        f"<b>Transaction Hash:</b> <code>{html.escape(normalized)}</code>\n\n"
        "Your transaction information has been transmitted to the PAWNS verification desk.\n"
        "An administrator will verify the payment. Once approved, the bot will automatically proceed to your service onboarding."
    )

    await message.reply_text(
        user_reply,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )

    await notify_admins(context, submission)
    context.user_data.pop("registration", None)
    return ConversationHandler.END


def admin_submission_text(submission: dict[str, Any]) -> str:
    username = submission.get("telegram_username")
    username_text = f"@{username}" if username else "Not set"
    plan = submission.get("selected_plan", {})
    plan_lines = []
    for key, value in plan.items():
        plan_lines.append(f"• {key.replace('_', ' ').title()}: {html.escape(str(value))}")

    chain_ver = submission.get("chain_verification", {})
    chain_status = chain_ver.get("status", "unknown")
    chain_details = chain_ver.get("details", "No check recorded")
    explorer_url = submission.get("explorer_url", "")
    explorer_link = f"<a href=\"{explorer_url}\">View on Explorer</a>" if explorer_url else "N/A"

    return (
        "🆕 <b>PAWNS PAYMENT VERIFICATION REQUIRED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{html.escape(submission['reference'])}</code>\n"
        f"<b>User:</b> {html.escape(submission.get('full_name', 'Unknown'))}\n"
        f"<b>Telegram:</b> {html.escape(username_text)}\n"
        f"<b>Telegram ID:</b> <code>{submission['telegram_id']}</code>\n"
        f"<b>Service:</b> {html.escape(submission.get('service_name', submission.get('service', '')))}\n"
        f"<b>Amount:</b> ${html.escape(str(submission.get('amount', '')))} {html.escape(submission.get('currency', ''))}\n"
        f"<b>Network:</b> {html.escape(submission.get('network', ''))}\n"
        f"<b>Destination Wallet:</b> <code>{html.escape(submission.get('wallet_address', ''))}</code>\n"
        f"<b>TXID:</b> <code>{html.escape(submission.get('txid', ''))}</code>\n"
        f"<b>Explorer:</b> {explorer_link}\n"
        f"<b>On-Chain Check:</b> <i>[{chain_status}] {html.escape(chain_details)}</i>\n"
        + ("\n".join(plan_lines) + "\n" if plan_lines else "")
        + f"<b>Payment Status:</b> <b>{html.escape(submission.get('payment_status', 'PENDING'))}</b>\n"
        f"<b>Onboarding Status:</b> {html.escape(submission.get('onboarding_status', 'Pending'))}"
    )


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, submission: dict[str, Any]) -> None:
    settings = get_settings(context)
    reference = submission["reference"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"admin:verify:{reference}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"admin:reject:{reference}"),
            ],
            [
                InlineKeyboardButton("ℹ️ Request Info", callback_data=f"admin:reqinfo:{reference}"),
            ],
        ]
    )
    for admin_id in settings.admin_chat_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_submission_text(submission),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except (Forbidden, BadRequest, TelegramError) as exc:
            LOGGER.error("Could not notify admin %s: %s", admin_id, exc)


async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("registration", None)
    context.user_data.pop("onboarding", None)
    await send_or_edit(
        update,
        main_menu_text(),
        main_menu_keyboard(),
    )
    return ConversationHandler.END


async def admin_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings = get_settings(context)
    admin_user = update.effective_user
    if not is_admin(admin_user.id if admin_user else None, settings):
        await query.answer("You are not authorised to perform this action.", show_alert=True)
        return

    _, action, reference = query.data.split(":", 2)
    db = get_db(context)
    now = utc_now()

    if action == "reqinfo":
        submission = await db.submissions.find_one({"reference": reference})
        if not submission:
            await query.answer("Submission not found.", show_alert=True)
            return
        await query.answer("Information request sent to user.")
        support_url = await get_link(context, "support")
        req_text = (
            "ℹ️ <b>PAWNS Payment Verification Notice</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"The verification desk requires additional information regarding your payment for "
            f"<b>{html.escape(submission.get('service_name', submission['service']))}</b> "
            f"(Ref: <code>{reference}</code>).\n\n"
            f"Please verify that your transaction was successfully broadcast on the "
            f"<b>{html.escape(submission.get('network', ''))}</b> network to PAWNS wallet "
            f"<code>{html.escape(submission.get('wallet_address', ''))}</code>.\n\n"
            "If your transaction failed or you have a corrected TXID, please reach out to PAWNS Support."
        )
        req_buttons = []
        if is_http_url(support_url):
            req_buttons.append([InlineKeyboardButton("🛟 Contact PAWNS Support", url=support_url)])
        req_buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")])
        try:
            await context.bot.send_message(
                chat_id=submission["telegram_id"],
                text=req_text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(req_buttons),
            )
        except TelegramError as exc:
            LOGGER.warning("Could not send reqinfo to user %s: %s", submission["telegram_id"], exc)
        return

    payment_status = "VERIFIED ✅" if action == "verify" else "REJECTED"
    onboarding_status = "Pending Details" if action == "verify" else "Rejected"

    submission = await db.submissions.find_one_and_update(
        {"reference": reference, "payment_status": "PENDING"},
        {
            "$set": {
                "payment_status": payment_status,
                "onboarding_status": onboarding_status,
                "admin_verification_status": {
                    "decision": "Approved" if action == "verify" else "Rejected",
                    "reviewed_by": admin_user.id,
                    "reviewed_at": now,
                },
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not submission:
        current = await db.submissions.find_one({"reference": reference})
        curr_status = current.get("payment_status", "not found") if current else "not found"
        await query.answer(f"No change made. Current status: {curr_status}", show_alert=True)
        return

    await db.audit.insert_one(
        {
            "action": f"payment_{action}",
            "reference": reference,
            "admin_id": admin_user.id,
            "created_at": now,
        }
    )
    await query.answer(f"Payment marked {payment_status}.")

    try:
        await query.edit_message_text(
            admin_submission_text(submission)
            + f"\n\n<b>Decision:</b> {payment_status}\n"
            f"<b>Reviewed by:</b> {html.escape(admin_user.full_name)}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest:
        pass

    service = submission["service"]
    service_name = submission.get("service_name", SERVICE_NAMES.get(service, service))

    if action == "verify":
        onboarding_prompts = {
            "crypto": "Please tap below to submit your <b>BingX UID</b> to complete your trading setup.",
            "forex_live": "Please tap below to submit your <b>Broker Name and Live Account ID</b>.",
            "forex_prop": "Please tap below to submit your <b>Prop Firm Name and Account/Challenge ID</b>.",
            "synthetic": "Please tap below to submit your <b>Synthetic Trading Account ID</b>.",
            "private": "Please tap below to confirm your <b>Investment Agreement & Onboarding Details</b>.",
        }
        prompt_text = onboarding_prompts.get(service, "Please tap below to submit your onboarding details.")
        user_message = (
            f"🎉 <b>Payment Status: VERIFIED ✅</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your payment of <b>${html.escape(str(submission.get('amount', '')))} "
            f"{html.escape(submission.get('currency', ''))}</b> for <b>{html.escape(service_name)}</b> "
            f"(Ref: <code>{reference}</code>) has been confirmed!\n\n"
            f"👉 <b>Next Step — Service Onboarding:</b>\n"
            f"{prompt_text}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Complete Onboarding", callback_data=f"onboard_start:{reference}")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
            ]
        )
    else:
        support_url = await get_link(context, "support")
        user_message = (
            f"❌ <b>Payment Status: REJECTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your payment for <b>{html.escape(service_name)}</b> (Ref: <code>{reference}</code>) was not approved.\n\n"
            "Please contact support with your transaction details or to submit a corrected payment reference."
        )
        buttons = []
        if is_http_url(support_url):
            buttons.append([InlineKeyboardButton("🛟 Contact PAWNS Support", url=support_url)])
        buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")])
        keyboard = InlineKeyboardMarkup(buttons)

    try:
        await context.bot.send_message(
            chat_id=submission["telegram_id"],
            text=user_message,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError as exc:
        LOGGER.warning("Could not notify user %s of payment review result: %s", submission["telegram_id"], exc)


async def onboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ref = query.data.split(":", 1)[1]
    db = get_db(context)
    submission = await db.submissions.find_one({"reference": ref, "telegram_id": update.effective_user.id})
    if not submission:
        await query.edit_message_text("Registration not found or already completed.")
        return ConversationHandler.END

    service = submission["service"]
    prompts = {
        "crypto": "Enter your BingX UID:",
        "forex_live": "Enter the broker name and your non-sensitive account identifier:",
        "forex_prop": "Enter the prop-firm name and your non-sensitive account identifier:",
        "synthetic": "Enter the provider name and your non-sensitive account identifier:",
        "private": "Confirm your legal name and agreement acceptance:",
    }
    context.user_data["onboarding"] = {"reference": ref, "service": service}
    await query.edit_message_text(
        f"<b>{html.escape(submission.get('service_name', service))} — Onboarding</b>\n\n"
        f"{prompts.get(service, 'Enter your account details:')}\n\n"
        "⚠️ Do not send passwords, private keys, seed phrases, OTPs, or authentication codes.",
        parse_mode=ParseMode.HTML,
    )
    return ONBOARDING_INPUT


async def receive_onboarding_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    text = clip(message.text or "", 300)
    if len(text) < 2:
        await message.reply_text("Please enter valid onboarding details.")
        return ONBOARDING_INPUT
    if FORBIDDEN_SECRET_RE.search(text):
        await message.reply_text(
            "For your security, do not submit passwords, seed phrases, private keys, OTPs, or authentication codes.\n"
            "Send only non-sensitive account identifiers / UIDs."
        )
        return ONBOARDING_INPUT

    onboard_data = context.user_data.get("onboarding", {})
    ref = onboard_data.get("reference")
    db = get_db(context)
    now = utc_now()
    updated = await db.submissions.find_one_and_update(
        {"reference": ref, "telegram_id": update.effective_user.id},
        {
            "$set": {
                "onboarding_status": "Completed",
                "onboarding_data": {"details": text, "submitted_at": now},
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    context.user_data.pop("onboarding", None)

    if not updated:
        await message.reply_text("Could not update onboarding record. Please contact support.")
        return ConversationHandler.END

    await message.reply_text(
        f"✅ <b>Onboarding Complete!</b>\n\n"
        f"Your onboarding details for <b>{html.escape(updated.get('service_name', ''))}</b> (Ref: <code>{ref}</code>) have been recorded.\n\n"
        "Our team will finalize your account setup. Thank you for choosing PAWNS!",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )

    for admin_id in get_settings(context).admin_chat_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📋 <b>ONBOARDING DETAILS SUBMITTED</b>\n\n"
                    f"<b>Reference:</b> <code>{ref}</code>\n"
                    f"<b>User:</b> {html.escape(update.effective_user.full_name)} (@{update.effective_user.username})\n"
                    f"<b>Service:</b> {html.escape(updated.get('service_name', ''))}\n"
                    f"<b>Details:</b> {html.escape(text)}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    return ConversationHandler.END


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_admin(update.effective_user.id if update.effective_user else None, settings):
        return
    db = get_db(context)
    users = await db.users.count_documents({})
    pending = await db.submissions.count_documents({"payment_status": {"$in": ["PENDING", "Under Review"]}})
    verified = await db.submissions.count_documents({"payment_status": {"$in": ["VERIFIED ✅", "Verified"]}})
    rejected = await db.submissions.count_documents({"payment_status": {"$in": ["REJECTED", "Rejected"]}})
    await update.effective_message.reply_text(
        "<b>PAWNS ADMIN STATS</b>\n\n"
        f"Registered users: {users}\n"
        f"Pending payments: {pending}\n"
        f"Verified payments: {verified}\n"
        f"Rejected payments: {rejected}",
        parse_mode=ParseMode.HTML,
    )


async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_admin(update.effective_user.id if update.effective_user else None, settings):
        return
    db = get_db(context)

    inv_wallet = await db.get_setting("investment_wallet", settings.investment_wallet)
    trd_wallet = await db.get_setting("trading_wallet", settings.trading_wallet)
    inv_net = await db.get_setting("investment_network", settings.investment_network)
    trd_net = await db.get_setting("trading_network", settings.trading_network)
    fee_crypto = await db.get_setting("fee_crypto", str(settings.fee_crypto))
    fee_forex_live = await db.get_setting("fee_forex_live", str(settings.fee_forex_live))
    fee_forex_prop = await db.get_setting("fee_forex_prop", str(settings.fee_forex_prop))
    fee_synthetic = await db.get_setting("fee_synthetic", str(settings.fee_synthetic))
    min_invest = await db.get_setting("minimum_investment", str(settings.minimum_investment))
    inst_inv = await db.get_setting("payment_instructions_investment", settings.payment_instructions_investment)
    inst_trd = await db.get_setting("payment_instructions_trading", settings.payment_instructions_trading)

    text = (
        "⚙️ <b>PAWNS ADMIN CONFIGURATION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Investment Wallet ({inv_net}):</b>\n<code>{inv_wallet}</code>\n\n"
        f"<b>Trading Services Wallet ({trd_net}):</b>\n<code>{trd_wallet}</code>\n\n"
        "<b>Service Fees:</b>\n"
        f"• Crypto Futures: ${fee_crypto}\n"
        f"• Forex Live: ${fee_forex_live}\n"
        f"• Forex Prop: ${fee_forex_prop}\n"
        f"• Synthetic Trading: ${fee_synthetic}\n"
        f"• Minimum Investment: ${min_invest}\n\n"
        "<b>Payment Instructions:</b>\n"
        f"• Investment: {inst_inv}\n"
        f"• Trading: {inst_trd}\n\n"
        "<b>Admin Commands:</b>\n"
        "• <code>/setwallet &lt;investment|trading&gt; &lt;address&gt;</code>\n"
        "• <code>/setfee &lt;crypto|forex_live|forex_prop|synthetic&gt; &lt;amount&gt;</code>\n"
        "• <code>/setmininvest &lt;amount&gt;</code>\n"
        "• <code>/setnetwork &lt;investment|trading&gt; &lt;network&gt;</code>\n"
        "• <code>/setinstructions &lt;investment|trading&gt; &lt;text&gt;</code>\n"
        "• <code>/audit</code>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def admin_set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    admin_id = update.effective_user.id if update.effective_user else None
    if not is_admin(admin_id, settings):
        return

    if not context.args or len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage: /setwallet &lt;investment|trading&gt; &lt;wallet_address&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    target, address = context.args[0].lower(), context.args[1].strip()
    if target not in {"investment", "trading"}:
        await update.effective_message.reply_text("Target must be 'investment' or 'trading'.")
        return

    db = get_db(context)
    network_key = "investment_network" if target == "investment" else "trading_network"
    current_net = await db.get_setting(
        network_key,
        settings.investment_network if target == "investment" else settings.trading_network,
    )

    if not validate_wallet_format(address, current_net):
        await update.effective_message.reply_text(
            f"❌ Invalid wallet address format for {current_net}.\n"
            f"TRC20 addresses start with 'T' (34 chars). BSC addresses start with '0x' (42 chars)."
        )
        return

    setting_key = "investment_wallet" if target == "investment" else "trading_wallet"
    audit_rec = await db.set_setting(setting_key, address, admin_id)
    await update.effective_message.reply_text(
        f"✅ Updated <b>{target}</b> wallet address.\n\n"
        f"<b>Previous:</b> <code>{html.escape(audit_rec.get('old_value', ''))}</code>\n"
        f"<b>New:</b> <code>{html.escape(address)}</code>\n\n"
        "Change recorded in admin audit log.",
        parse_mode=ParseMode.HTML,
    )


async def admin_set_fee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    admin_id = update.effective_user.id if update.effective_user else None
    if not is_admin(admin_id, settings):
        return

    if not context.args or len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage: /setfee &lt;crypto|forex_live|forex_prop|synthetic&gt; &lt;amount&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    svc, amount_str = context.args[0].lower(), context.args[1].strip().replace("$", "")
    valid_keys = {"crypto", "forex_live", "forex_prop", "synthetic"}
    if svc not in valid_keys:
        await update.effective_message.reply_text(f"Service must be one of: {', '.join(valid_keys)}")
        return

    try:
        amt = Decimal(amount_str).quantize(Decimal("0.01"))
        if amt < 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Amount must be a non-negative number.")
        return

    db = get_db(context)
    audit_rec = await db.set_setting(f"fee_{svc}", str(amt), admin_id)
    await update.effective_message.reply_text(
        f"✅ Updated <b>{svc}</b> service fee to <b>${amt}</b> (was ${audit_rec.get('old_value', '')}).\n"
        "Change recorded in audit log.",
        parse_mode=ParseMode.HTML,
    )


async def admin_set_min_invest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    admin_id = update.effective_user.id if update.effective_user else None
    if not is_admin(admin_id, settings):
        return

    if not context.args or len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /setmininvest &lt;amount&gt;", parse_mode=ParseMode.HTML)
        return

    amount_str = context.args[0].strip().replace("$", "")
    try:
        amt = Decimal(amount_str).quantize(Decimal("0.01"))
        if amt <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Amount must be a positive number.")
        return

    db = get_db(context)
    audit_rec = await db.set_setting("minimum_investment", str(amt), admin_id)
    await update.effective_message.reply_text(
        f"✅ Updated minimum investment to <b>${amt}</b> (was ${audit_rec.get('old_value', '')}).",
        parse_mode=ParseMode.HTML,
    )


async def admin_set_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    admin_id = update.effective_user.id if update.effective_user else None
    if not is_admin(admin_id, settings):
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /setnetwork &lt;investment|trading&gt; &lt;network_name&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    target = context.args[0].lower()
    if target not in {"investment", "trading"}:
        await update.effective_message.reply_text("Target must be 'investment' or 'trading'.")
        return

    net_name = " ".join(context.args[1:]).strip()
    db = get_db(context)
    key = "investment_network" if target == "investment" else "trading_network"
    await db.set_setting(key, net_name, admin_id)
    await update.effective_message.reply_text(f"✅ Updated {target} network to: {net_name}")


async def admin_set_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    admin_id = update.effective_user.id if update.effective_user else None
    if not is_admin(admin_id, settings):
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /setinstructions &lt;investment|trading&gt; &lt;instructions text&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    target = context.args[0].lower()
    if target not in {"investment", "trading"}:
        await update.effective_message.reply_text("Target must be 'investment' or 'trading'.")
        return

    inst_text = " ".join(context.args[1:]).strip()
    db = get_db(context)
    key = "payment_instructions_investment" if target == "investment" else "payment_instructions_trading"
    await db.set_setting(key, inst_text, admin_id)
    await update.effective_message.reply_text(f"✅ Updated {target} payment instructions.")


async def admin_audit_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_admin(update.effective_user.id if update.effective_user else None, settings):
        return

    db = get_db(context)
    cursor = db.audit.find({}).sort("created_at", -1).limit(10)
    lines = ["📜 <b>ADMIN AUDIT LOG (Last 10 Events)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    async for entry in cursor:
        dt = entry.get("created_at")
        dt_str = dt.strftime("%Y-%m-%d %H:%M UTC") if isinstance(dt, datetime) else str(dt)
        action = entry.get("action", "unknown")
        setting = entry.get("setting", "")
        old_val = entry.get("old_value", "")
        new_val = entry.get("new_value", "")
        admin = entry.get("admin_id", "")
        if setting:
            lines.append(f"• <b>{dt_str}</b>: <code>{action}</code> on <code>{setting}</code> by Admin <code>{admin}</code>\n  Old: <code>{clip(str(old_val), 30)}</code> → New: <code>{clip(str(new_val), 30)}</code>")
        else:
            ref = entry.get("reference", "")
            lines.append(f"• <b>{dt_str}</b>: <code>{action}</code> (Ref: <code>{ref}</code>) by <code>{admin}</code>")

    await update.effective_message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


async def admin_set_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_admin(update.effective_user.id if update.effective_user else None, settings):
        return
    if not context.args or len(context.args) != 2:
        keys = ", ".join(LINK_SETTING_KEYS)
        await update.effective_message.reply_text(f"Usage: /setlink &lt;key&gt; &lt;https-url&gt;\nKeys: {keys}", parse_mode=ParseMode.HTML)
        return
    key, url = context.args
    key = key.lower()
    if key not in LINK_SETTING_KEYS:
        await update.effective_message.reply_text("Unknown link key.")
        return
    if not is_http_url(url):
        await update.effective_message.reply_text("The link must be a valid HTTP or HTTPS URL.")
        return
    await get_db(context).set_setting(key, url, update.effective_user.id)
    await update.effective_message.reply_text(f"Updated {key} link.")


async def admin_add_commission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if not is_admin(update.effective_user.id if update.effective_user else None, settings):
        return
    if not context.args or len(context.args) < 3:
        await update.effective_message.reply_text(
            "Usage: /addcommission &lt;referrer_telegram_id&gt; &lt;affiliate_commission&gt; &lt;currency&gt; [note]",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        referrer_id = int(context.args[0])
        affiliate_commission = Decimal(context.args[1])
    except (ValueError, InvalidOperation):
        await update.effective_message.reply_text("Telegram ID must be an integer and commission must be numeric.")
        return
    if affiliate_commission <= 0:
        await update.effective_message.reply_text("Commission must be greater than zero.")
        return
    if not await get_db(context).users.find_one({"telegram_id": referrer_id}):
        await update.effective_message.reply_text("Referrer was not found.")
        return
    share = (affiliate_commission * settings.commission_percent / Decimal("100")).quantize(Decimal("0.01"))
    event_reference = make_reference("COM")
    await get_db(context).commissions.insert_one(
        {
            "reference": event_reference,
            "referrer_telegram_id": referrer_id,
            "affiliate_commission": str(affiliate_commission),
            "commission_percent": str(settings.commission_percent),
            "referrer_share": str(share),
            "currency": context.args[2].upper()[:10],
            "note": clip(" ".join(context.args[3:]), 300),
            "status": "verified",
            "created_by": update.effective_user.id,
            "created_at": utc_now(),
        }
    )
    await update.effective_message.reply_text(
        f"Recorded verified referral earning: {context.args[2].upper()} {share}\nReference: {event_reference}"
    )


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "I did not understand that message. Use /menu to view the available services."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled bot error while processing update %r", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong while processing that request. Please try again or contact support."
            )
        except TelegramError:
            pass


async def post_init(application: Application) -> None:
    db: Database = application.bot_data["db"]
    await db.initialize()
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("menu", "Return to the main menu"),
            BotCommand("services", "Display all services"),
            BotCommand("investment", "Open private investment"),
            BotCommand("crypto", "Open crypto futures onboarding"),
            BotCommand("forex", "Open forex onboarding"),
            BotCommand("synthetic", "Open synthetic onboarding"),
            BotCommand("referral", "View the referral program"),
            BotCommand("support", "Contact support"),
            BotCommand("terms", "View terms and risk disclosure"),
            BotCommand("cancel", "Cancel the current registration"),
        ]
    )
    if db.is_memory_mode:
        LOGGER.info("Bot @%s connected; running in IN-MEMORY test mode (no MongoDB)", me.username)
    else:
        LOGGER.info("Bot @%s connected; database indexes ready", me.username)


async def post_shutdown(application: Application) -> None:
    await application.bot_data["db"].close()


def build_application(settings: Settings) -> Application:
    db = Database(settings)
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(False)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db

    registration = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(registration_start, pattern=r"^register:(private|crypto|forex_live|forex_prop|synthetic)$"),
            CallbackQueryHandler(onboard_start, pattern=r"^onboard_start:BM-\d{8}-[A-F0-9]{8}$"),
        ],
        states={
            FULL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_full_name)],
            INVESTMENT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_investment_amount)],
            RISK_CATEGORY: [CallbackQueryHandler(select_risk, pattern=r"^risk:(high|low)$")],
            DURATION: [CallbackQueryHandler(select_duration, pattern=r"^duration:(2m|3m|6m|12m)$")],
            CONSENT: [CallbackQueryHandler(receive_consent, pattern=r"^consent:(yes|no)$")],
            PAYMENT_DETAILS: [CallbackQueryHandler(receive_payment_button, pattern=r"^pay:(confirm|cancel)$")],
            AWAIT_TXID: [
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    receive_txid,
                )
            ],
            ONBOARDING_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_onboarding_input)],
        },
        fallbacks=[
            CommandHandler(["cancel", "menu", "start", "stop"], cancel_registration),
        ],
        allow_reentry=True,
        name="registration",
    )
    application.add_handler(registration)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["menu", "services", "cancel", "stop"], show_main_menu))
    application.add_handler(CommandHandler("investment", show_private))
    application.add_handler(CommandHandler("crypto", show_crypto))
    application.add_handler(CommandHandler("forex", show_forex))
    application.add_handler(CommandHandler("synthetic", show_synthetic))
    application.add_handler(CommandHandler("referral", show_referral))
    application.add_handler(CommandHandler("support", show_support))
    application.add_handler(CommandHandler("terms", show_terms))

    # Admin commands
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CommandHandler(["settings", "admin"], admin_settings))
    application.add_handler(CommandHandler("setwallet", admin_set_wallet))
    application.add_handler(CommandHandler("setfee", admin_set_fee))
    application.add_handler(CommandHandler("setmininvest", admin_set_min_invest))
    application.add_handler(CommandHandler("setnetwork", admin_set_network))
    application.add_handler(CommandHandler("setinstructions", admin_set_instructions))
    application.add_handler(CommandHandler(["audit", "auditlog"], admin_audit_log))
    application.add_handler(CommandHandler("setlink", admin_set_link))
    application.add_handler(CommandHandler("addcommission", admin_add_commission))

    application.add_handler(
        CallbackQueryHandler(
            admin_review,
            pattern=r"^admin:(verify|reject|reqinfo):BM-\d{8}-[A-F0-9]{8}$",
        )
    )
    application.add_handler(CallbackQueryHandler(not_configured, pattern=r"^not_configured:"))
    application.add_handler(
        CallbackQueryHandler(
            route_menu_callback,
            pattern=r"^(menu|about|support|terms|referral|service:(private|plans|crypto|forex|forex_live|forex_prop|synthetic))$",
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    settings = Settings.from_env()
    application = build_application(settings)
    allowed_updates = Update.ALL_TYPES
    if settings.run_mode == "webhook":
        webhook_url = f"{settings.webhook_base_url}/{settings.webhook_path}"
        application.run_webhook(
            listen="0.0.0.0",
            port=settings.port,
            url_path=settings.webhook_path,
            webhook_url=webhook_url,
            secret_token=settings.webhook_secret,
            allowed_updates=allowed_updates,
            drop_pending_updates=False,
        )
    else:
        application.run_polling(allowed_updates=allowed_updates, drop_pending_updates=False)


if __name__ == "__main__":
    main()
