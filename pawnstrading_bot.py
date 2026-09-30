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

import asyncio
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
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urlparse

from pymongo import ASCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, PyMongoError, ServerSelectionTimeoutError
from pymongo.server_api import ServerApi
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
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


# Conversation states for Registration & Payment
(
    FULL_NAME,
    INVESTMENT_AMOUNT,
    RISK_CATEGORY,
    DURATION,
    CONSENT,
    SELECT_PAYMENT_METHOD,
    PAYMENT_DETAILS,
    AWAIT_TXID,
    AWAIT_NAIRA_RECEIPT,
    ONBOARDING_INPUT,
) = range(10)

# Standalone conversation states
(BINGX_UID_INPUT,) = range(10, 11)
(INVESTOR_WITHDRAW_INPUT,) = range(20, 21)
(INVESTOR_TERMINATE_INPUT,) = range(30, 31)
(ADMIN_REPORT_INPUT,) = range(40, 41)
(ANNOUNCEMENT_TEXT_INPUT, ANNOUNCEMENT_CONFIRM) = range(50, 52)
(ADMIN_VERIFY_LINK_INPUT,) = range(60, 61)


SERVICE_NAMES = {
    "private": "PAWNS Private Investment",
    "crypto": "Crypto Futures Trading",
    "forex_live": "Forex Live Account Trading",
    "forex_prop": "Forex Prop Firm Trading",
    "synthetic": "Synthetic Trading",
}

# Crypto Futures Fee Schedules
CRYPTO_BINGX_FEES = {
    "1m": Decimal("40"),
    "3m": Decimal("90"),
    "6m": Decimal("200"),
    "12m": Decimal("300"),
}

CRYPTO_STANDARD_FEES = {
    "1m": Decimal("70"),
    "3m": Decimal("149.9"),
    "6m": Decimal("250"),
    "12m": Decimal("300"),
}

FOREX_FEES = {
    "1m": Decimal("50"),
    "3m": Decimal("70"),
    "6m": Decimal("150"),
    "12m": Decimal("200"),
}

DURATION_DAYS = {
    "1m": 30,
    "2m": 60,
    "3m": 90,
    "6m": 180,
    "12m": 365,
}

SERVICE_FEES = {
    "crypto": "$40 - $300 (BingX) / $70 - $300 (Other Exchanges)",
    "forex_live": "$50 - $200 depending on duration",
    "forex_prop": "$50 - $200 depending on duration (separate from challenge fees)",
    "synthetic": "Coming soon",
}

INVESTMENT_PLANS = {
    "high": {"2m": "50%", "3m": "100%", "6m": "200%", "12m": "400%"},
    "low": {"2m": "20%", "3m": "50%", "6m": "100%", "12m": "200%"},
}

DURATION_LABELS = {
    "1m": "1 month",
    "2m": "2 months",
    "3m": "3 months",
    "6m": "6 months",
    "12m": "1 year",
}

# Tiered Referral System Schedules (Paid Referrals Only)
# Format: (min_paid_referrals, commission_rate_percentage)
TRADING_REFERRAL_TIERS = [
    (100, Decimal("25")),  # 100+ paid referrals -> 25%
    (50, Decimal("20")),   # 50-99 paid referrals -> 20%
    (25, Decimal("15")),   # 25-49 paid referrals -> 15%
    (15, Decimal("12")),   # 15-24 paid referrals -> 12%
    (5, Decimal("10")),    # 5-14 paid referrals  -> 10%
    (0, Decimal("7")),     # 0-4 paid referrals   -> 7% (Base)
]
INVESTMENT_REFERRAL_RATE = Decimal("10")  # 10% of referral profits for private investment


def get_trading_referral_tier(paid_count: int) -> tuple[Decimal, int, int | None]:
    """
    Returns (rate_percentage, current_tier_min, next_tier_min).
    Example for paid_count=7: returns (Decimal("10"), 5, 15).
    """
    for idx, (min_refs, rate) in enumerate(TRADING_REFERRAL_TIERS):
        if paid_count >= min_refs:
            next_tier_min = TRADING_REFERRAL_TIERS[idx - 1][0] if idx > 0 else None
            return rate, min_refs, next_tier_min
    return Decimal("7"), 0, 5


LINK_SETTING_KEYS = {
    "bingx": "BINGX_URL",
    "broker": "BROKER_URL",
    "prop": "PROP_FIRM_URL",
    "synthetic": "SYNTHETIC_URL",
    "support": "SUPPORT_URL",
    "terms": "TERMS_URL",
    "investment_terms": "INVESTMENT_TERMS_URL",
    "pawns_channel": "PAWNS_CHANNEL_URL",
    "investor_portal_bot": "INVESTOR_PORTAL_BOT_URL",
    "broker_1": "BROKER_1_URL",
    "broker_2": "BROKER_2_URL",
    "broker_3": "BROKER_3_URL",
    "prop_1": "PROP_FIRM_1_URL",
    "prop_2": "PROP_FIRM_2_URL",
    "prop_3": "PROP_FIRM_3_URL",
}

USER_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("menu", "Return to main menu"),
    BotCommand("investment", "Open private investment"),
    BotCommand("crypto", "Open crypto futures onboarding"),
    BotCommand("forex", "Open forex onboarding"),
    BotCommand("synthetic", "Open synthetic onboarding"),
    BotCommand("referral", "View referral program"),
    BotCommand("support", "Contact support (@Moyin_13)"),
    BotCommand("terms", "View terms and risk disclosure"),
    BotCommand("myid", "Check your Telegram ID & status"),
    BotCommand("cancel", "Cancel current registration"),
]

ADMIN_COMMANDS = [
    BotCommand("admin", "Admin Control Center"),
    BotCommand("announcement", "Broadcast announcement to all users"),
    BotCommand("stats", "Live platform statistics"),
    BotCommand("admins", "List authorized administrators"),
    BotCommand("addadmin", "Grant administrator privileges"),
    BotCommand("removeadmin", "Revoke administrator privileges"),
    BotCommand("settings", "View dynamic platform configuration"),
    BotCommand("checkexpiry", "Check subscription expiries"),
    BotCommand("audit", "Inspect recent audit log"),
    BotCommand("report", "Export transactions & reports"),
] + USER_COMMANDS


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


def normalize_invite_link(raw: str) -> str | None:
    link = raw.strip()
    if not link:
        return None
    if not (link.startswith("http://") or link.startswith("https://")):
        if link.startswith("t.me/"):
            link = "https://" + link
        elif link.startswith("+"):
            link = "https://t.me/" + link
        elif "t.me/" in link:
            link = "https://" + link[link.find("t.me/"):]
        else:
            link = "https://" + link
    if not is_http_url(link):
        return None
    parsed = urlparse(link)
    if "." not in parsed.netloc:
        return None
    return link


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
    usd_ngn_rate: Decimal = Decimal("1400")
    naira_bank_name: str = "Zenith Bank"
    naira_account_number: str = "1234567890"
    naira_account_name: str = "PAWNS Trading Services"
    pawns_channel_url: str = "https://t.me/pawns_channel_placeholder"
    investor_portal_bot_url: str = "https://t.me/pawns_investor_bot_placeholder"
    broker_1_name: str = "Exness"
    broker_1_url: str = "https://example.com/exness-partner"
    broker_2_name: str = "HFM (HotForex)"
    broker_2_url: str = "https://example.com/hfm-partner"
    broker_3_name: str = "Deriv Forex"
    broker_3_url: str = "https://example.com/deriv-partner"
    prop_1_name: str = "Naira Trader"
    prop_1_url: str = "https://example.com/nairatrader"
    prop_2_name: str = "Naira Prop"
    prop_2_url: str = "https://www.nairaprop.com/?ref=USER1D63"
    prop_3_name: str = "Global Dollar Prop"
    prop_3_url: str = "https://example.com/dollarprop"

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

        try:
            usd_ngn_rate = Decimal(os.getenv("USD_NGN_RATE", "1400"))
        except InvalidOperation:
            usd_ngn_rate = Decimal("1400")

        naira_bank = os.getenv("NAIRA_BANK_NAME", "Zenith Bank").strip()
        naira_acc_num = os.getenv("NAIRA_ACCOUNT_NUMBER", "1234567890").strip()
        naira_acc_name = os.getenv("NAIRA_ACCOUNT_NAME", "PAWNS Trading Services").strip()

        pawns_channel = os.getenv("PAWNS_CHANNEL_URL", "https://t.me/pawns_channel_placeholder").strip()
        investor_bot = os.getenv("INVESTOR_PORTAL_BOT_URL", "https://t.me/pawns_investor_bot_placeholder").strip()

        broker_1_name = os.getenv("BROKER_1_NAME", "Exness").strip()
        broker_1_url = os.getenv("BROKER_1_URL", "https://example.com/exness-partner").strip()
        broker_2_name = os.getenv("BROKER_2_NAME", "HFM (HotForex)").strip()
        broker_2_url = os.getenv("BROKER_2_URL", "https://example.com/hfm-partner").strip()
        broker_3_name = os.getenv("BROKER_3_NAME", "Deriv Forex").strip()
        broker_3_url = os.getenv("BROKER_3_URL", "https://example.com/deriv-partner").strip()

        prop_1_name = os.getenv("PROP_FIRM_1_NAME", "Naira Trader").strip()
        prop_1_url = os.getenv("PROP_FIRM_1_URL", "https://example.com/nairatrader").strip()
        prop_2_name = os.getenv("PROP_FIRM_2_NAME", "Naira Prop").strip()
        prop_2_url = os.getenv("PROP_FIRM_2_URL", "https://www.nairaprop.com/?ref=USER1D63").strip()
        prop_3_name = os.getenv("PROP_FIRM_3_NAME", "Global Dollar Prop").strip()
        prop_3_url = os.getenv("PROP_FIRM_3_URL", "https://example.com/dollarprop").strip()

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
            usd_ngn_rate=usd_ngn_rate,
            naira_bank_name=naira_bank,
            naira_account_number=naira_acc_num,
            naira_account_name=naira_acc_name,
            pawns_channel_url=pawns_channel,
            investor_portal_bot_url=investor_bot,
            broker_1_name=broker_1_name,
            broker_1_url=broker_1_url,
            broker_2_name=broker_2_name,
            broker_2_url=broker_2_url,
            broker_3_name=broker_3_name,
            broker_3_url=broker_3_url,
            prop_1_name=prop_1_name,
            prop_1_url=prop_1_url,
            prop_2_name=prop_2_name,
            prop_2_url=prop_2_url,
            prop_3_name=prop_3_name,
            prop_3_url=prop_3_url,
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



def _get_nested(doc: dict[str, Any], key: str) -> Any:
    if "." in key:
        parts = key.split(".")
        current = doc
        for part in parts:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current
    return doc.get(key)


def _set_nested(doc: dict[str, Any], key: str, value: Any) -> None:
    if "." in key:
        parts = key.split(".")
        current = doc
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = copy.deepcopy(value)
    else:
        doc[key] = copy.deepcopy(value)


def _matches(doc: dict[str, Any], filter_dict: dict[str, Any]) -> bool:
    for k, v in filter_dict.items():
        if k == "$or":
            if not any(_matches(doc, cond) for cond in v):
                return False
            continue
        doc_val = _get_nested(doc, k)
        if isinstance(v, dict):
            if "$exists" in v:
                exists = doc_val is not None
                if exists != v["$exists"]:
                    return False
            if "$in" in v:
                if doc_val not in v["$in"]:
                    return False
            if "$ne" in v:
                if doc_val == v["$ne"]:
                    return False
            if "$lte" in v:
                if doc_val is None or doc_val > v["$lte"]:
                    return False
            if "$gte" in v:
                if doc_val is None or doc_val < v["$gte"]:
                    return False
            if "$gt" in v:
                if doc_val is None or doc_val <= v["$gt"]:
                    return False
            if "$lt" in v:
                if doc_val is None or doc_val >= v["$lt"]:
                    return False
        else:
            if doc_val != v:
                return False
    return True


def _apply_update(doc: dict[str, Any], update_dict: dict[str, Any], is_insert: bool = False) -> None:
    if "$set" in update_dict:
        for k, v in update_dict["$set"].items():
            _set_nested(doc, k, v)
    if is_insert and "$setOnInsert" in update_dict:
        for k, v in update_dict["$setOnInsert"].items():
            _set_nested(doc, k, v)


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

    def find(self, filter_dict: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> AsyncCursorWrapper:
        f = filter_dict or {}
        matched = [copy.deepcopy(d) for d in self.docs if _matches(d, f)]
        return AsyncCursorWrapper(matched)

    async def count_documents(self, filter_dict: dict[str, Any]) -> int:
        return sum(1 for d in self.docs if _matches(d, filter_dict))

    async def aggregate(self, pipeline: list[dict[str, Any]]) -> AsyncCursorWrapper:
        match_stage: dict[str, Any] = {}
        group_field = "currency"
        for stage in pipeline:
            if "$match" in stage:
                match_stage = stage["$match"]
            if "$group" in stage:
                raw_id = stage["$group"].get("_id", "$currency")
                if isinstance(raw_id, str) and raw_id.startswith("$"):
                    group_field = raw_id[1:]
        matched = [d for d in self.docs if _matches(d, match_stage)]
        grouped: dict[str, Decimal] = {}
        for d in matched:
            if group_field == "program":
                key = str(d.get("program") or ("private_investment" if d.get("service") == "private" else "trading_subscriptions"))
            else:
                key = str(d.get(group_field, "USD"))
            share = Decimal(str(d.get("referrer_share", "0")))
            grouped[key] = grouped.get(key, Decimal("0")) + share
        results = [{"_id": key, "total": total} for key, total in grouped.items()]
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
            self.subscriptions = self.db.subscriptions
            self.withdrawals = self.db.withdrawals
            self.reports = self.db.reports
            self.bingx_verifications = self.db.bingx_verifications
            self.terminations = self.db.terminations
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
        self.subscriptions = InMemoryCollection("subscriptions")
        self.withdrawals = InMemoryCollection("withdrawals")
        self.reports = InMemoryCollection("reports")
        self.bingx_verifications = InMemoryCollection("bingx_verifications")
        self.terminations = InMemoryCollection("terminations")

    async def initialize(self) -> None:
        if self.is_memory_mode:
            LOGGER.warning("Running with IN-MEMORY storage (no MongoDB). Data will NOT persist across restarts.")
            return

        try:
            await self.client.admin.command({"ping": 1})
            await self.users.create_index("telegram_id", unique=True)
            await self.users.create_index("referral_id", unique=True)
            await self.users.create_index("referred_by")
            await self.users.create_index([("referred_by", ASCENDING), ("is_paid_referral", ASCENDING)])
            await self.submissions.create_index("reference", unique=True)
            await self.submissions.create_index([("telegram_id", ASCENDING), ("created_at", ASCENDING)])
            await self.submissions.create_index("payment_status")
            await self.submissions.create_index("txid")
            await self.commissions.create_index([("referrer_telegram_id", ASCENDING), ("status", ASCENDING)])
            await self.audit.create_index("created_at")
            await self.subscriptions.create_index("telegram_id")
            await self.subscriptions.create_index("status")
            await self.subscriptions.create_index("expires_at")
            await self.withdrawals.create_index("reference", unique=True)
            await self.reports.create_index("reference", unique=True)
            await self.bingx_verifications.create_index("telegram_id")
            await self.terminations.create_index("reference", unique=True)
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
                    "is_paid_referral": False,
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
                    "is_paid_referral": False,
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
    duration: str | None = None,
    track: str | None = None,
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
        if duration is None:
            if service == "crypto":
                amount = await db.get_setting("fee_crypto", str(settings.fee_crypto))
            elif service == "forex_live":
                amount = await db.get_setting("fee_forex_live", str(settings.fee_forex_live))
            elif service == "forex_prop":
                amount = await db.get_setting("fee_forex_prop", str(settings.fee_forex_prop))
            elif service == "synthetic":
                amount = await db.get_setting("fee_synthetic", str(settings.fee_synthetic))
            else:
                amount = str(settings.fee_crypto)
        else:
            dur_key = duration
            if service == "crypto":
                if track == "bingx":
                    amount = str(CRYPTO_BINGX_FEES.get(dur_key, Decimal("40")))
                else:
                    amount = str(CRYPTO_STANDARD_FEES.get(dur_key, Decimal("70")))
            elif service in ("forex_live", "forex_prop"):
                amount = str(FOREX_FEES.get(dur_key, Decimal("50")))
            elif service == "synthetic":
                amount = await db.get_setting("fee_synthetic", str(settings.fee_synthetic))
            else:
                amount = str(settings.fee_crypto)

    return {
        "service": service,
        "service_name": SERVICE_NAMES.get(service, service),
        "amount": amount,
        "currency": currency,
        "network": network,
        "wallet": wallet,
        "instructions": instructions,
        "duration": duration,
        "track": track,
    }


def render_payment_method_screen(
    service_name: str,
    amount_usd: str,
    duration_label: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    dur_text = f"<b>Duration:</b> {html.escape(duration_label)}\n" if duration_label else ""
    text = (
        "💳 <b>SELECT PAYMENT METHOD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Service:</b> {html.escape(service_name)}\n"
        f"{dur_text}"
        f"<b>Amount:</b> ${html.escape(str(amount_usd))} USD\n\n"
        "Select your preferred payment method below:"
    )
    buttons = [
        [InlineKeyboardButton("🌐 Crypto (USDT)", callback_data="paymethod:crypto")],
        [InlineKeyboardButton("🇳🇬 Naira (Bank Transfer)", callback_data="paymethod:naira")],
        [InlineKeyboardButton("❌ Cancel", callback_data="pay:cancel")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def render_crypto_payment_screen(payment_info: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "💳 <b>CRYPTO PAYMENT DETAILS</b>\n"
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


def render_naira_payment_screen(
    payment_info: dict[str, Any],
    settings: Settings,
    usd_rate: Decimal,
) -> tuple[str, InlineKeyboardMarkup]:
    usd_amount = Decimal(payment_info["amount"])
    ngn_amount = int(usd_amount * usd_rate)
    bank_name = settings.naira_bank_name
    acc_num = settings.naira_account_number
    acc_name = settings.naira_account_name
    dur_label = DURATION_LABELS.get(payment_info.get("duration", ""), "")
    dur_text = f"<b>Duration:</b> {html.escape(dur_label)}\n" if dur_label else ""

    text = (
        "🇳🇬 <b>NAIRA BANK PAYMENT INSTRUCTIONS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Service:</b> {html.escape(payment_info['service_name'])}\n"
        f"{dur_text}"
        f"<b>USD Equivalent:</b> ${usd_amount} USD\n"
        f"<b>Exchange Rate:</b> ₦{usd_rate:,} / $1 USD\n"
        f"<b>Amount Payable:</b> <b>₦{ngn_amount:,} NGN</b>\n\n"
        "<b>PAWNS Bank Details:</b>\n"
        f"• <b>Bank Name:</b> {html.escape(bank_name)}\n"
        f"• <b>Account Number:</b> <code>{html.escape(acc_num)}</code>\n"
        f"• <b>Account Name:</b> {html.escape(acc_name)}\n\n"
        "⚠️ <b>INSTRUCTIONS:</b>\n"
        "1. Transfer the exact amount shown above to the designated account.\n"
        "2. Tap <b>'📤 I've Transferred (Send Receipt)'</b> below.\n"
        "3. Send your transaction screenshot or receipt as an image or document."
    )
    buttons = [
        [InlineKeyboardButton("📤 I've Transferred (Send Receipt)", callback_data="pay:confirm_naira")],
        [InlineKeyboardButton("📋 Copy Account Number", copy_text=CopyTextButton(text=acc_num))],
        [InlineKeyboardButton("❌ Cancel", callback_data="pay:cancel")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def render_payment_screen(payment_info: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    return render_crypto_payment_screen(payment_info)


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def is_admin(user_id: int | None, settings: Settings, extra_admins: set[int] | None = None) -> bool:
    if user_id is None:
        return False
    if user_id in settings.admin_chat_ids:
        return True
    if extra_admins and user_id in extra_admins:
        return True
    return False


def get_admin_ids(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    settings = get_settings(context)
    admins = set(settings.admin_chat_ids)
    cached = context.application.bot_data.get("admin_ids")
    if cached:
        admins.update(cached)
    return admins


def is_admin_user(user_id: int | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id is None:
        return False
    return user_id in get_admin_ids(context)


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    user_id = user.id if user else None
    if not is_admin_user(user_id, context):
        if update.callback_query:
            await update.callback_query.answer("⛔ Access denied: Not an administrator.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(
                "⛔ <b>Access Denied</b>\n\n"
                f"Your Telegram ID (<code>{user_id or 'Unknown'}</code>) is not recognized as an administrator.\n\n"
                "To authorize this account, ask an existing administrator to run:\n"
                f"<code>/addadmin {user_id}</code>\n"
                "or add this ID to <code>ADMIN_CHAT_IDS</code> in your environment.",
                parse_mode=ParseMode.HTML,
            )
        return False
    return True


def referral_id_for(telegram_id: int, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), str(telegram_id).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"BIT{digest[:10].upper()}"


def main_menu_keyboard(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("♟️ Private Investment", callback_data="service:private")],
        [InlineKeyboardButton("📈 Crypto Futures", callback_data="service:crypto")],
        [InlineKeyboardButton("💱 Forex Trading", callback_data="service:forex")],
        [InlineKeyboardButton("📊 Synthetic Trading (Coming Soon)", callback_data="service:synthetic")],
        [InlineKeyboardButton("🤝 Referral Program", callback_data="referral")],
        [
            InlineKeyboardButton("ℹ️ About", callback_data="about"),
            InlineKeyboardButton("🛟 Support", callback_data="support"),
        ],
        [InlineKeyboardButton("📄 Terms & Risk Disclosure", callback_data="terms")],
    ]
    if is_admin_user:
        buttons.append([InlineKeyboardButton("🛠 Admin Control Center", callback_data="admin:menu")])
    return InlineKeyboardMarkup(buttons)


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
    db = get_db(context)
    user_doc = await db.upsert_user(
        user,
        referral_id_for(user.id, settings.referral_secret),
    )
    is_env_admin = user.id in settings.admin_chat_ids
    if is_env_admin:
        if not user_doc.get("is_admin"):
            await db.users.update_one(
                {"telegram_id": user.id},
                {"$set": {"is_admin": True}},
            )
            user_doc["is_admin"] = True
        context.application.bot_data.setdefault("admin_ids", set()).add(user.id)
    elif user_doc.get("is_admin"):
        context.application.bot_data.setdefault("admin_ids", set()).add(user.id)

    return user_doc


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

    is_adm = is_admin_user(update.effective_user.id if update.effective_user else None, context)
    await send_or_edit(update, main_menu_text(), main_menu_keyboard(is_admin_user=is_adm))


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update, context)
    context.user_data.pop("registration", None)
    is_adm = is_admin_user(update.effective_user.id if update.effective_user else None, context)
    await send_or_edit(
        update,
        main_menu_text(),
        main_menu_keyboard(is_admin_user=is_adm),
    )


def configurable_link_button(label: str, url: str, missing_key: str) -> InlineKeyboardButton:
    if is_http_url(url):
        return InlineKeyboardButton(label, url=url)
    return InlineKeyboardButton(f"{label} (not configured)", callback_data=f"not_configured:{missing_key}")


async def get_link(context: ContextTypes.DEFAULT_TYPE, short_key: str) -> str:
    env_name = LINK_SETTING_KEYS[short_key]
    val = await get_db(context).get_setting(short_key, os.getenv(env_name, "").strip())
    if short_key == "support":
        cleaned = val.strip()
        if not cleaned or "REPLACE_WITH" in cleaned or "placeholder" in cleaned:
            return "https://t.me/Moyin_13"
        if cleaned.startswith("@"):
            return f"https://t.me/{cleaned[1:]}"
        if not cleaned.startswith("http://") and not cleaned.startswith("https://"):
            if cleaned.startswith("t.me/"):
                return f"https://{cleaned}"
            return f"https://t.me/{cleaned}"
        return cleaned
    return val


async def show_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db = get_db(context)
    user_doc = await db.users.find_one({"telegram_id": user.id}) if user else None
    is_active_investor = bool(user_doc and user_doc.get("investor_status") == "active")

    if is_active_investor:
        inv = user_doc.get("investor_details", {})
        amt = inv.get("amount", "0")
        risk = str(inv.get("risk", "Low")).title()
        dur = inv.get("duration", "N/A")
        ret = inv.get("proposed_return", "N/A")
        start_date = inv.get("start_date", "")
        start_str = start_date.strftime("%Y-%m-%d") if isinstance(start_date, datetime) else str(start_date)[:10] if start_date else "Active"
        ref = inv.get("reference", "N/A")

        text = (
            "♟️ <b>PAWNS INVESTOR HUB</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Welcome back, <b>{html.escape(user.full_name)}</b>!\n\n"
            "<b>Your Active Investment Portfolio:</b>\n"
            f"• <b>Reference:</b> <code>{html.escape(ref)}</code>\n"
            f"• <b>Invested Principal:</b> ${html.escape(str(amt))} USDT\n"
            f"• <b>Risk Profile:</b> {html.escape(risk)}\n"
            f"• <b>Duration:</b> {html.escape(str(dur))}\n"
            f"• <b>Proposed Return:</b> {html.escape(str(ret))}\n"
            f"• <b>Started:</b> {html.escape(start_str)}\n"
            "• <b>Status:</b> <code>ACTIVE ✅</code>\n\n"
            "Use the options below to manage your investment or contact support:"
        )
        support_url = await get_link(context, "support")
        buttons = [
            [InlineKeyboardButton("📊 Request Report", callback_data="inv:report")],
            [InlineKeyboardButton("💸 Request Withdrawal", callback_data="inv:withdraw")],
            [InlineKeyboardButton("📄 Termination of Contract", callback_data="inv:terminate")],
        ]
        if is_http_url(support_url):
            buttons.append([InlineKeyboardButton("💬 Talk to Team / Support", url=support_url)])
        buttons.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="menu")])
        await send_or_edit(update, text, InlineKeyboardMarkup(buttons))
        return

    settings = get_settings(context)
    start_label = "💰 Invest Now" if settings.private_investment_enabled else "🔒 Registration not yet enabled"
    start_callback = "register:private" if settings.private_investment_enabled else "not_configured:private_investment"
    buttons = [
        [InlineKeyboardButton(start_label, callback_data=start_callback)],
        [InlineKeyboardButton("📊 View Investment Plans", callback_data="service:plans")],
        [InlineKeyboardButton("📄 Investment Terms & Risk Policy", callback_data="terms:investment")],
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
    text = (
        "📈 <b>PAWNS CRYPTO FUTURES TRADING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Access high-accuracy PAWNS crypto futures trading signals and updates.\n\n"
        "<b>Available Tracks:</b>\n"
        "• <b>BingX Users:</b> Special discounted pricing from <b>$40/month</b> to <b>$300/year</b>.\n"
        "• <b>Other Exchanges:</b> Standard pricing from <b>$100/month</b>.\n\n"
        "Select an option below to proceed:"
    )
    support_url = await get_link(context, "support")
    buttons = [
        [InlineKeyboardButton("📋 View Service Fees & Pricing", callback_data="crypto:fees")],
        [InlineKeyboardButton("⚡ BingX User Track (Discounted)", callback_data="crypto:bingx_start")],
        [InlineKeyboardButton("🌐 Other Exchanges Track (Standard)", callback_data="crypto:standard_start")],
    ]
    if is_http_url(support_url):
        buttons.append([InlineKeyboardButton("🛟 Contact Support", url=support_url)])
    buttons.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="menu")])
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_crypto_fees(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📊 <b>CRYPTO FUTURES SERVICE FEES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ <b>BingX Registered Users (Discounted Rates):</b>\n"
        "• 1 Month: <b>$40</b>\n"
        "• 3 Months: <b>$90</b>\n"
        "• 6 Months: <b>$200</b>\n"
        "• 1 Year: <b>$300</b>\n\n"
        "🌐 <b>Other Exchanges (Standard Rates):</b>\n"
        "• 1 Month: <b>$70</b>\n"
        "• 3 Months: <b>$149.9</b>\n"
        "• 6 Months: <b>$250</b>\n"
        "• 1 Year: <b>$300</b>\n\n"
        "<i>BingX users receive reduced fees by registering with our official link.</i>"
    )
    buttons = [
        [InlineKeyboardButton("⚡ Proceed with BingX Track", callback_data="crypto:bingx_start")],
        [InlineKeyboardButton("🌐 Proceed with Other Exchanges", callback_data="crypto:standard_start")],
        [InlineKeyboardButton("⬅️ Back", callback_data="service:crypto")],
    ]
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_crypto_bingx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db = get_db(context)
    user_doc = await db.users.find_one({"telegram_id": user.id}) if user else None
    is_bingx_verified = bool(user_doc and user_doc.get("bingx_verified"))

    if is_bingx_verified:
        uid_val = user_doc.get("bingx_uid", "Verified")
        text = (
            "⚡ <b>BINGX TRADING TRACK (VERIFIED)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your BingX account (UID: <code>{html.escape(str(uid_val))}</code>) is verified! ✅\n\n"
            "Choose your discounted subscription duration:"
        )
        buttons = [
            [InlineKeyboardButton("1 Month — $40", callback_data="cf_pay:bingx:1m")],
            [InlineKeyboardButton("3 Months — $90", callback_data="cf_pay:bingx:3m")],
            [InlineKeyboardButton("6 Months — $200", callback_data="cf_pay:bingx:6m")],
            [InlineKeyboardButton("1 Year — $300", callback_data="cf_pay:bingx:12m")],
            [InlineKeyboardButton("⬅️ Back", callback_data="service:crypto")],
        ]
        await send_or_edit(update, text, InlineKeyboardMarkup(buttons))
        return

    bingx_url = await get_link(context, "bingx")
    support_url = await get_link(context, "support")
    text = (
        "⚡ <b>BINGX TRADING TRACK</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Enjoy discounted PAWNS Crypto Futures fees by trading with our partnered exchange, BingX!\n\n"
        "<b>Steps:</b>\n"
        "1. Register on BingX using our official partner link.\n"
        "2. Submit your BingX UID for quick verification.\n"
        "3. Once verified, unlock discounted rates (from $40/mo or $300/yr)."
    )
    buttons = [
        [configurable_link_button("🔗 Register on BingX", bingx_url, "bingx")],
        [InlineKeyboardButton("📋 Already Registered? Enter UID", callback_data="bingx:enter_uid")],
    ]
    if is_http_url(support_url):
        buttons.append([InlineKeyboardButton("🛟 Contact Support", url=support_url)])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="service:crypto")])
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_crypto_standard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🌐 <b>OTHER EXCHANGES — STANDARD TRACK</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Trade PAWNS crypto futures on your preferred exchange (Binance, Bybit, OKX, etc.).\n\n"
        "Choose your subscription duration below to proceed to payment:"
    )
    buttons = [
        [InlineKeyboardButton("1 Month — $70", callback_data="cf_pay:standard:1m")],
        [InlineKeyboardButton("3 Months — $149.9", callback_data="cf_pay:standard:3m")],
        [InlineKeyboardButton("6 Months — $250", callback_data="cf_pay:standard:6m")],
        [InlineKeyboardButton("1 Year — $300", callback_data="cf_pay:standard:12m")],
        [InlineKeyboardButton("⬅️ Back", callback_data="service:crypto")],
    ]
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_forex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit(
        update,
        "💱 <b>PAWNS FOREX TRADING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select your preferred forex onboarding track below.\n\n"
        "• <b>Live Account Trading:</b> Connect with our affiliated brokers.\n"
        "• <b>Prop Firm Trading:</b> Pass challenges and trade funded accounts with our partner prop firms.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📈 Live Account Trading", callback_data="service:forex_live")],
                [InlineKeyboardButton("🏆 Prop Firm Trading", callback_data="service:forex_prop")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
            ]
        ),
    )


async def show_forex_live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    b1_url = await get_link(context, "broker_1")
    b2_url = await get_link(context, "broker_2")

    buttons = [
        [configurable_link_button(f"🔗 {settings.broker_1_name}", b1_url, "broker_1")],
        [configurable_link_button(f"🔗 {settings.broker_2_name}", b2_url, "broker_2")],
        [InlineKeyboardButton("💳 Subscribe / Pay Service Fee", callback_data="forex_live:durations")],
        [InlineKeyboardButton("⬅️ Back", callback_data="service:forex")],
    ]
    await send_or_edit(
        update,
        "💱 <b>FOREX LIVE ACCOUNT TRADING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Register with one of our affiliated partner brokers below, or proceed directly to pay your service fee.\n\n"
        "⚠️ <i>Never send your trading account password, private key, or OTPs.</i>",
        InlineKeyboardMarkup(buttons),
    )


async def show_forex_prop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    p1_url = await get_link(context, "prop_1")
    p2_url = await get_link(context, "prop_2")

    buttons = [
        [configurable_link_button(f"🔗 {settings.prop_1_name}", p1_url, "prop_1")],
        [configurable_link_button(f"🔗 {settings.prop_2_name}", p2_url, "prop_2")],
        [InlineKeyboardButton("💳 Subscribe / Pay Service Fee", callback_data="forex_prop:durations")],
        [InlineKeyboardButton("⬅️ Back", callback_data="service:forex")],
    ]
    await send_or_edit(
        update,
        "🏆 <b>FOREX PROP FIRM TRADING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Get funded with our partnered prop firms below, or proceed directly to pay your PAWNS onboarding service fee.\n\n"
        "<i>Prop firm challenge fees are payable under that provider's platform.</i>",
        InlineKeyboardMarkup(buttons),
    )


async def show_forex_durations(update: Update, context: ContextTypes.DEFAULT_TYPE, forex_type: str) -> None:
    title = "Live Account" if forex_type == "live" else "Prop Firm"
    back_target = f"service:forex_{forex_type}"
    text = (
        f"💳 <b>FOREX {title.upper()} — SELECT DURATION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Choose your subscription period to proceed to payment:"
    )
    buttons = [
        [InlineKeyboardButton("1 Month — $50", callback_data=f"forex_pay:{forex_type}:1m")],
        [InlineKeyboardButton("3 Months — $70", callback_data=f"forex_pay:{forex_type}:3m")],
        [InlineKeyboardButton("6 Months — $150", callback_data=f"forex_pay:{forex_type}:6m")],
        [InlineKeyboardButton("1 Year — $200", callback_data=f"forex_pay:{forex_type}:12m")],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_target)],
    ]
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_synthetic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit(
        update,
        "📊 <b>PAWNS SYNTHETIC TRADING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Synthetic trading onboarding is currently undergoing maintenance and will be available soon.</i>\n\n"
        "Stay tuned to our official announcements!",
        back_keyboard("menu"),
    )


async def show_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit(update, f"<b>ABOUT PAWNS</b>\n\n{html.escape(get_settings(context).about_text)}", back_keyboard())


async def show_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📜 <b>PAWNS TERMS OF SERVICE &amp; RISK DISCLOSURE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Please review the governing terms and disclosures below:</i>\n\n"
        "<b>1. Scope of Services &amp; Platform Role</b>\n"
        "PAWNS provides market educational content, trade analysis, and technical onboarding "
        "for independent third-party brokers and proprietary trading firms. PAWNS does not operate "
        "as a custodian of user trading funds or a registered broker-dealer.\n\n"
        "<b>2. Financial Risk &amp; Leverage Warning</b>\n"
        "Trading foreign exchange (Forex), cryptocurrencies, synthetic contracts, and leveraged products "
        "carries substantial risk of loss. Leverage magnifies both potential gains and losses. You may "
        "lose some or all of your deposited capital. Never risk funds you cannot afford to lose.\n\n"
        "<b>3. No Personalized Financial Advice</b>\n"
        "All channel broadcasts, signals, and bot guidance represent general market technical commentary "
        "and do NOT constitute individualized investment, tax, or legal advice. You maintain sole "
        "responsibility for your trading decisions.\n\n"
        "<b>4. Third-Party Provider Independence</b>\n"
        "Partner brokers (e.g. Exness, HFM) and prop firms (e.g. Naira Trader, Naira Prop) operate "
        "independently. PAWNS assumes no liability for their order execution, platform latency, spread "
        "fluctuations, challenge rules, or withdrawal processing.\n\n"
        "<b>5. Service Fees &amp; Non-Refundability</b>\n"
        "Onboarding and VIP signal fees cover immediate provisioning of intellectual property. "
        "Once verified and access is granted, all fee payments are final and non-refundable.\n\n"
        "<b>6. Security Notice</b>\n"
        "PAWNS staff will <b>NEVER</b> ask for your trading account password, private key, wallet seed phrase, "
        "or OTP codes. Official payments must strictly follow verified in-bot instructions."
    )
    buttons = [
        [InlineKeyboardButton("♟️ View Private Investment Terms", callback_data="terms:investment")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")],
    ]
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_investment_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "♟️ <b>PAWNS PRIVATE INVESTMENT TERMS &amp; RISK POLICY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Review the terms governing private capital allocation:</i>\n\n"
        "<b>1. Capital Allocation &amp; Portfolio Custody</b>\n"
        "Funds deposited under the PAWNS Private Investment Program are deployed into actively managed "
        "algorithmic and discretionary trading strategies aligned with the selected risk profile.\n\n"
        "<b>2. Minimum Allocation &amp; Commitments</b>\n"
        "• <b>Minimum Principal:</b> $500 USDT (strictly TRON TRC20 network).\n"
        "• <b>Duration Cycles:</b> Commitments (e.g., 3, 6, 12 months) are required for strategy execution. "
        "Early contract termination requires administrative review.\n\n"
        "<b>3. Profit Distribution &amp; Return Basis</b>\n"
        "Target percentages reflect net profit distributions based on closed trading P&amp;L. "
        "Profits may be withdrawn at cycle completion through the in-bot Investor Hub. "
        "Principal return or rollover is executed upon maturity reconciliation.\n\n"
        "<b>4. Market Risk &amp; Volatility Disclosure</b>\n"
        "Despite strict risk parameters (such as drawdown stops and position limits), private portfolio "
        "allocation remains subject to market volatility. Invested capital is not insured by governmental "
        "deposit schemes.\n\n"
        "<b>5. Governance</b>\n"
        "This policy operates in conjunction with the bilateral investor agreement confirmed upon deposit. "
        "In any discrepancy, signed records and blockchain confirmations prevail."
    )
    buttons = [
        [InlineKeyboardButton("⬅️ Back to Private Investment", callback_data="service:private")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu")],
    ]
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    support_url = await get_link(context, "support")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Contact Support (@Moyin_13)", url=support_url)],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")],
        ]
    )
    text = (
        "🛟 <b>PAWNS SUPPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Have questions about onboarding, verification, or our trading services?\n\n"
        "Reach our official support administrator directly on Telegram: <b>@Moyin_13</b>\n\n"
        "Click the button below to start a direct message."
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

    # 1. Total Attributed Referrals (free + paid)
    total_attributed = await db.users.count_documents({"referred_by": user_id})

    # 2. Qualified Paid Referrals (only users whose payments have been verified)
    paid_count = await db.users.count_documents({
        "referred_by": user_id,
        "is_paid_referral": True,
    })

    # 3. Dynamic Tier Calculation for Trading Subscriptions
    current_rate, current_min, next_min = get_trading_referral_tier(paid_count)
    if next_min is not None:
        needed = next_min - paid_count
        next_rate = next((r for m, r in TRADING_REFERRAL_TIERS if m == next_min), Decimal("25"))
        progress_text = f"<b>Next Tier:</b> {needed} more paid ref{'s' if needed != 1 else ''} to unlock <b>{next_rate}%</b>"
    else:
        progress_text = "<b>Next Tier:</b> 🏆 Max Tier Reached (25%)!"

    # 4. Aggregated Earnings per Program
    trading_earnings = Decimal("0")
    investment_earnings = Decimal("0")
    async for row in await db.commissions.aggregate([
        {"$match": {"referrer_telegram_id": user_id, "status": "verified"}},
        {"$group": {"_id": "$program", "total": {"$sum": {"$toDecimal": "$referrer_share"}}}},
    ]):
        prog = row.get("_id")
        tot = Decimal(str(row.get("total", "0")))
        if prog == "private_investment":
            investment_earnings += tot
        else:
            trading_earnings += tot
    total_earnings = trading_earnings + investment_earnings

    text = (
        "🤝 <b>PAWNS PARTNER & REFERRAL PROGRAM</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Earn lifetime commissions by introducing traders and investors to PAWNS.\n\n"
        "🔗 <b>Your Referral Details:</b>\n"
        f"• <b>Referral ID:</b> <code>{html.escape(user_doc['referral_id'])}</code>\n"
        f"• <b>Referral Link:</b> <code>{html.escape(referral_link)}</code>\n\n"
        "👥 <b>Your Referral Network:</b>\n"
        f"• <b>Total Referrals:</b> {total_attributed}\n"
        f"• <b>Qualified Paid Referrals:</b> <b>{paid_count}</b>\n\n"
        "⚡ <b>Trading Subscriptions (Crypto Futures & Forex):</b>\n"
        f"• <b>Current Commission Rate:</b> <b>{current_rate}%</b> of service fees\n"
        f"• {progress_text}\n\n"
        "♟️ <b>Private Investment:</b>\n"
        f"• <b>Commission Rate:</b> <b>{INVESTMENT_REFERRAL_RATE}%</b> of referred investor profits\n\n"
        "💰 <b>Your Verified Earnings:</b>\n"
        f"• <b>Trading Subscriptions:</b> ${trading_earnings:.2f} USDT\n"
        f"• <b>Private Investment Profits:</b> ${investment_earnings:.2f} USDT\n"
        f"• <b>Total Verified Earnings:</b> <b>${total_earnings:.2f} USDT</b>\n\n"
        "<i>Note: Commission rates and tier upgrades apply strictly to verified paid referrals.</i>"
    )
    buttons = [
        [InlineKeyboardButton("📋 Copy Referral Link", copy_text=CopyTextButton(text=referral_link))],
        [InlineKeyboardButton("📊 View Tier Schedule", callback_data="referral:tiers")],
        [InlineKeyboardButton("📄 Referral Terms", callback_data="terms")],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="menu")],
    ]
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


async def show_referral_tiers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📊 <b>PAWNS REFERRAL TIER SCHEDULE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ <b>Trading Subscriptions (Crypto Futures & Forex):</b>\n"
        "Commissions are paid as a percentage of service fees paid by your referrals:\n\n"
        "• <b>Base Tier (0 – 4 paid refs):</b> <b>7%</b>\n"
        "• <b>Tier 1 (5 – 14 paid refs):</b> <b>10%</b>\n"
        "• <b>Tier 2 (15 – 24 paid refs):</b> <b>12%</b>\n"
        "• <b>Tier 3 (25 – 49 paid refs):</b> <b>15%</b>\n"
        "• <b>Tier 4 (50 – 99 paid refs):</b> <b>20%</b>\n"
        "• <b>Tier 5 (100+ paid refs):</b> <b>25%</b>\n\n"
        "♟️ <b>Private Investment Program:</b>\n"
        "• <b>Flat Base Rate:</b> <b>10%</b> of realized referral profit.\n\n"
        "⚠️ <i>Tier qualifications and payouts strictly apply to unique paid referrals whose payments are confirmed and verified.</i>"
    )
    buttons = [
        [InlineKeyboardButton("⬅️ Back to Referral Hub", callback_data="referral")],
    ]
    await send_or_edit(update, text, InlineKeyboardMarkup(buttons))


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
        "crypto:fees": show_crypto_fees,
        "crypto:bingx_start": show_crypto_bingx,
        "crypto:standard_start": show_crypto_standard,
        "service:forex": show_forex,
        "service:forex_live": show_forex_live,
        "service:forex_prop": show_forex_prop,
        "forex_live:durations": lambda u, c: show_forex_durations(u, c, "live"),
        "forex_prop:durations": lambda u, c: show_forex_durations(u, c, "prop"),
        "service:synthetic": show_synthetic,
        "about": show_about,
        "support": show_support,
        "terms": show_terms,
        "terms:investment": show_investment_terms,
        "referral": show_referral,
        "referral:tiers": show_referral_tiers,
        "inv:report": investor_report_request,
        "admin:menu": lambda u, c: admin_panel_menu(u, c),
        "admin:stats": lambda u, c: admin_stats(u, c),
        "admin:pending": lambda u, c: admin_pending_callback(u, c),
        "admin:settings_view": lambda u, c: admin_settings_view(u, c),
        "admin:manage": lambda u, c: admin_manage_callback(u, c),
        "admin:audit": lambda u, c: admin_audit_callback(u, c),
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

    context.user_data["registration"] = {
        "service": service,
        "track": "standard",
        "duration_key": "1m",
        "duration": "1 month",
    }
    await query.edit_message_text(
        f"<b>{html.escape(SERVICE_NAMES[service])}</b>\n\n"
        "Please enter your name:\n\n"
        "Send /cancel at any time to stop this registration.",
        parse_mode=ParseMode.HTML,
    )
    return FULL_NAME


async def crypto_pay_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user(update, context)
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    track = parts[1]  # "bingx" or "standard"
    duration = parts[2]  # "1m", "3m", "6m", "12m"

    fee = CRYPTO_BINGX_FEES[duration] if track == "bingx" else CRYPTO_STANDARD_FEES[duration]
    duration_label = DURATION_LABELS.get(duration, duration)

    context.user_data["registration"] = {
        "service": "crypto",
        "track": track,
        "duration_key": duration,
        "duration": duration_label,
        "fee": str(fee),
    }
    track_title = "BingX VIP" if track == "bingx" else "Standard VIP"
    await query.edit_message_text(
        f"<b>PAWNS Crypto Futures — {track_title}</b>\n"
        f"Duration: <b>{duration_label}</b> (${fee} USD)\n\n"
        "Please enter your name to start registration:\n\n"
        "Send /cancel at any time to abort.",
        parse_mode=ParseMode.HTML,
    )
    return FULL_NAME


async def forex_pay_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user(update, context)
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    forex_type = parts[1]  # "live" or "prop"
    duration = parts[2]  # "1m", "3m", "6m", "12m"

    service = "forex_live" if forex_type == "live" else "forex_prop"
    fee = FOREX_FEES[duration]
    duration_label = DURATION_LABELS.get(duration, duration)

    context.user_data["registration"] = {
        "service": service,
        "track": forex_type,
        "duration_key": duration,
        "duration": duration_label,
        "fee": str(fee),
    }
    svc_name = SERVICE_NAMES[service]
    await query.edit_message_text(
        f"<b>{html.escape(svc_name)}</b>\n"
        f"Duration: <b>{duration_label}</b> (${fee} USD)\n\n"
        "Please enter your name to start registration:\n\n"
        "Send /cancel at any time to abort.",
        parse_mode=ParseMode.HTML,
    )
    return FULL_NAME


async def receive_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = clip(update.effective_message.text or "", 120)
    if len(name) < 2 or any(char.isdigit() for char in name):
        await update.effective_message.reply_text("Please enter a valid name (at least 2 letters, no numbers).")
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

    # For trading services, prompt payment method directly
    return await prompt_payment_method(update, context)


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
                [
                    InlineKeyboardButton("🔥 Higher Risk", callback_data="risk:high"),
                    InlineKeyboardButton("🛡️ Lower Risk", callback_data="risk:low"),
                ],
                [InlineKeyboardButton("ℹ️ What's Involved? / Risk Overview", callback_data="risk:info")],
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


async def show_risk_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    info_text = (
        "ℹ️ <b>UNDERSTANDING RISK CATEGORIES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔥 <b>Higher-Risk Objective:</b>\n"
        "• Targets higher yield multipliers (e.g. 50% for 2m up to 400% for 12m).\n"
        "• Uses aggressive trading models and higher market exposure.\n"
        "• Carries higher volatility and increased potential drawdown.\n\n"
        "🛡️ <b>Lower-Risk Objective:</b>\n"
        "• Targets balanced capital preservation and steady yield (e.g. 20% for 2m up to 200% for 12m).\n"
        "• Employs strict risk controls, lower position sizing, and tight stop-loss rules.\n"
        "• Best suited for conservative participants prioritizing downside protection.\n\n"
        "⚠️ <i>All figures represent proposed return targets, not guaranteed outcomes. Invest only what you are comfortable allocating.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔥 Select Higher Risk", callback_data="risk:high"),
                InlineKeyboardButton("🛡️ Select Lower Risk", callback_data="risk:low"),
            ],
            [InlineKeyboardButton("⬅️ Back to Risk Selection", callback_data="risk:back")],
        ]
    )
    await query.edit_message_text(info_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return RISK_CATEGORY


async def back_to_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    reg = context.user_data.get("registration", {})
    amount = reg.get("investment_amount", "0")
    await query.edit_message_text(
        f"Selected amount: <b>${amount}</b>\n\nChoose a risk category:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔥 Higher Risk", callback_data="risk:high"),
                    InlineKeyboardButton("🛡️ Lower Risk", callback_data="risk:low"),
                ],
                [InlineKeyboardButton("ℹ️ What's Involved? / Risk Overview", callback_data="risk:info")],
            ]
        ),
    )
    return RISK_CATEGORY


async def select_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    duration = query.data.split(":", 1)[1]
    registration = context.user_data["registration"]
    risk = registration["risk_category"]
    if duration not in INVESTMENT_PLANS[risk]:
        return DURATION
    registration["duration_key"] = duration
    registration["duration"] = DURATION_LABELS[duration]
    registration["proposed_return"] = INVESTMENT_PLANS[risk][duration]
    return await ask_for_consent(update, context)


def registration_summary(registration: dict[str, Any]) -> str:
    service = registration["service"]
    lines = [
        f"<b>Service:</b> {html.escape(SERVICE_NAMES.get(service, service))}",
        f"<b>Name:</b> {html.escape(registration['full_name'])}",
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
    return await prompt_payment_method(update, context)


async def prompt_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    registration = context.user_data.get("registration", {})
    service = registration.get("service", "private")
    dur_key = registration.get("duration_key", "1m")
    track = registration.get("track", "standard")
    inv_amt = registration.get("investment_amount")

    payment_info = await get_service_payment_info(
        context,
        service,
        investment_amount=inv_amt,
        duration=dur_key,
        track=track,
    )
    registration["payment_info"] = payment_info
    dur_label = registration.get("duration", DURATION_LABELS.get(dur_key, ""))

    # Naira bank transfer is strictly only available for Forex payments
    if service in ("forex_live", "forex_prop"):
        text, keyboard = render_payment_method_screen(
            payment_info["service_name"],
            payment_info["amount"],
            duration_label=dur_label,
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return SELECT_PAYMENT_METHOD
    else:
        # Private investment and crypto futures are strictly Crypto (USDT)
        registration["payment_method"] = "crypto"
        text, keyboard = render_crypto_payment_screen(payment_info)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return PAYMENT_DETAILS


async def receive_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    method = query.data.split(":", 1)[1]

    registration = context.user_data.get("registration", {})
    service = registration.get("service")

    if method == "naira" and service not in ("forex_live", "forex_prop"):
        await query.answer("Naira bank transfer is only available for Forex subscriptions.", show_alert=True)
        return SELECT_PAYMENT_METHOD

    registration["payment_method"] = method
    payment_info = registration.get("payment_info", {})
    settings = get_settings(context)
    db = get_db(context)

    if method == "crypto":
        text, keyboard = render_crypto_payment_screen(payment_info)
    else:
        usd_rate = Decimal(await db.get_setting("usd_ngn_rate", str(settings.usd_ngn_rate)))
        text, keyboard = render_naira_payment_screen(payment_info, settings, usd_rate)

    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
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

    if data == "pay:confirm_naira":
        registration = context.user_data.get("registration", {})
        service = registration.get("service")
        if service not in ("forex_live", "forex_prop"):
            await query.answer("Naira bank transfer is only available for Forex subscriptions.", show_alert=True)
            return PAYMENT_DETAILS
        await query.edit_message_text(
            "📤 <b>Upload Payment Receipt</b>\n\n"
            "Please upload your bank transfer payment receipt screenshot as a <b>photo</b> or <b>document</b>.\n\n"
            "Our finance desk will verify your payment manually once uploaded.",
            parse_mode=ParseMode.HTML,
        )
        return AWAIT_NAIRA_RECEIPT

    return PAYMENT_DETAILS


async def receive_naira_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    photo = message.photo[-1] if message.photo else None
    document = message.document if message.document else None

    if not photo and not document:
        await message.reply_text(
            "⚠️ Please upload your payment receipt as an image or document screenshot."
        )
        return AWAIT_NAIRA_RECEIPT

    registration = context.user_data.get("registration")
    if not registration or "payment_info" not in registration:
        await message.reply_text("Session expired. Please restart registration from /menu.")
        return ConversationHandler.END

    if registration.get("service") not in ("forex_live", "forex_prop"):
        await message.reply_text("⚠️ Naira payments are strictly accepted only for Forex services.")
        return ConversationHandler.END

    payment_info = registration["payment_info"]
    file_id = photo.file_id if photo else document.file_id
    file_type = "photo" if photo else "document"

    db = get_db(context)
    user = update.effective_user
    reference = make_reference()
    now = utc_now()
    user_doc = await db.users.find_one({"telegram_id": user.id})

    settings = get_settings(context)
    usd_rate = Decimal(await db.get_setting("usd_ngn_rate", str(settings.usd_ngn_rate)))
    usd_amount = Decimal(payment_info["amount"])
    ngn_amount = int(usd_amount * usd_rate)

    submission = {
        "reference": reference,
        "telegram_id": user.id,
        "telegram_username": user.username,
        "full_name": registration.get("full_name", user.full_name),
        "service": payment_info["service"],
        "service_name": payment_info["service_name"],
        "duration_key": registration.get("duration_key", "1m"),
        "track": registration.get("track", "standard"),
        "amount": str(usd_amount),
        "currency": "USD",
        "payment_method": "naira",
        "ngn_amount": ngn_amount,
        "usd_ngn_rate": str(usd_rate),
        "receipt_file_id": file_id,
        "receipt_file_type": file_type,
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
        {"action": "naira_payment_submitted", "reference": reference, "telegram_id": user.id, "created_at": now}
    )

    user_reply = (
        "⏳ <b>Payment Status: PENDING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{reference}</code>\n"
        f"<b>Service:</b> {html.escape(payment_info['service_name'])}\n"
        f"<b>Amount:</b> ₦{ngn_amount:,} (${usd_amount} USD)\n"
        f"<b>Payment Method:</b> Naira Bank Transfer\n\n"
        "Your payment receipt screenshot has been received and forwarded to our finance desk.\n"
        "An administrator will verify your payment and activate your service shortly."
    )
    await message.reply_text(user_reply, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())

    await notify_admins_naira(context, submission)
    context.user_data.pop("registration", None)
    return ConversationHandler.END


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
        "duration_key": registration.get("duration_key", "1m"),
        "track": registration.get("track", "standard"),
        "amount": str(payment_info["amount"]),
        "currency": payment_info["currency"],
        "network": network,
        "wallet_address": payment_info["wallet"],
        "txid": normalized,
        "payment_method": "crypto",
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

    if submission.get("payment_method") == "naira":
        return (
            "🆕 <b>PAWNS NAIRA PAYMENT VERIFICATION REQUIRED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Reference:</b> <code>{html.escape(submission['reference'])}</code>\n"
            f"<b>User:</b> {html.escape(submission.get('full_name', 'Unknown'))}\n"
            f"<b>Telegram:</b> {html.escape(username_text)}\n"
            f"<b>Telegram ID:</b> <code>{submission['telegram_id']}</code>\n"
            f"<b>Service:</b> {html.escape(submission.get('service_name', submission.get('service', '')))}\n"
            f"<b>Amount:</b> ₦{submission.get('ngn_amount', 0):,} (${html.escape(str(submission.get('amount', '')))} USD)\n"
            f"<b>Payment Method:</b> Naira Bank Transfer\n"
            + ("\n".join(plan_lines) + "\n" if plan_lines else "")
            + f"<b>Payment Status:</b> <b>{html.escape(submission.get('payment_status', 'PENDING'))}</b>\n"
            f"<b>Onboarding Status:</b> {html.escape(submission.get('onboarding_status', 'Pending'))}"
        )

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


def make_admin_submission_keyboard(reference: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
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


async def edit_admin_review_message(
    message: Any,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if not message:
        return
    try:
        if getattr(message, "photo", None) or getattr(message, "document", None):
            await message.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception as exc:
        LOGGER.warning("Could not edit admin review message: %s", exc)


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, submission: dict[str, Any]) -> None:
    settings = get_settings(context)
    reference = submission["reference"]
    keyboard = make_admin_submission_keyboard(reference)
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


async def notify_admins_naira(context: ContextTypes.DEFAULT_TYPE, submission: dict[str, Any]) -> None:
    settings = get_settings(context)
    reference = submission["reference"]
    keyboard = make_admin_submission_keyboard(reference)
    caption = (
        "🆕 <b>PAWNS NAIRA PAYMENT VERIFICATION REQUIRED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{html.escape(reference)}</code>\n"
        f"<b>User:</b> {html.escape(submission.get('full_name', 'Unknown'))}\n"
        f"<b>Telegram:</b> @{html.escape(submission.get('telegram_username', 'Not set'))}\n"
        f"<b>Telegram ID:</b> <code>{submission['telegram_id']}</code>\n"
        f"<b>Service:</b> {html.escape(submission.get('service_name', ''))}\n"
        f"<b>Amount:</b> ₦{submission.get('ngn_amount', 0):,} (${submission.get('amount', '')} USD)\n"
        f"<b>Payment Method:</b> Naira Bank Transfer\n"
        f"<b>Status:</b> <b>PENDING</b>\n\n"
        "<i>Payment receipt attached:</i>"
    )
    file_id = submission.get("receipt_file_id")
    file_type = submission.get("receipt_file_type", "photo")

    for admin_id in settings.admin_chat_ids:
        try:
            if file_id and file_type == "photo":
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            elif file_id and file_type == "document":
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            else:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
        except (Forbidden, BadRequest, TelegramError) as exc:
            LOGGER.error("Could not notify admin %s of Naira payment: %s", admin_id, exc)


async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("registration", None)
    context.user_data.pop("onboarding", None)
    await send_or_edit(
        update,
        main_menu_text(),
        main_menu_keyboard(),
    )
    return ConversationHandler.END


async def process_referral_on_payment_verified(
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict[str, Any],
) -> dict[str, Any] | None:
    db = get_db(context)
    user_id = submission.get("telegram_id")
    if not user_id:
        return None

    user_doc = await db.users.find_one({"telegram_id": user_id})
    if not user_doc:
        return None

    referrer_id = user_doc.get("referred_by")
    if not referrer_id:
        return None

    referrer = await db.users.find_one({"telegram_id": referrer_id})
    if not referrer:
        return None

    now = utc_now()
    service = submission.get("service", "")
    reference = submission.get("reference", "")

    # Mark user as paid referral if not already marked
    is_first_paid = not user_doc.get("is_paid_referral")
    if is_first_paid:
        await db.users.update_one(
            {"telegram_id": user_id},
            {"$set": {"is_paid_referral": True, "first_paid_at": now, "updated_at": now}},
        )

    # Count referrer's unique qualified paid referrals
    paid_count = await db.users.count_documents({
        "referred_by": referrer_id,
        "is_paid_referral": True,
    })

    if service in ("crypto", "forex_live", "forex_prop", "synthetic"):
        rate, current_min, next_min = get_trading_referral_tier(paid_count)
        try:
            payment_amount = Decimal(str(submission.get("amount", "0")))
        except (InvalidOperation, ValueError):
            payment_amount = Decimal("0")

        if payment_amount > 0:
            share = (payment_amount * rate / Decimal("100")).quantize(Decimal("0.01"))
            com_ref = make_reference("COM")
            service_name = submission.get("service_name", SERVICE_NAMES.get(service, service))
            com_doc = {
                "reference": com_ref,
                "referrer_telegram_id": referrer_id,
                "referred_telegram_id": user_id,
                "program": "trading_subscriptions",
                "service": service,
                "service_name": service_name,
                "payment_reference": reference,
                "payment_amount": str(payment_amount),
                "commission_rate": str(rate),
                "paid_referrals_at_time": paid_count,
                "referrer_share": str(share),
                "currency": "USDT",
                "status": "verified",
                "created_at": now,
            }
            await db.commissions.insert_one(com_doc)

            try:
                next_tier_text = f" ({next_min - paid_count} more paid referrals to reach next tier)" if next_min else " (Top tier achieved!)"
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=(
                        "🎉 <b>Referral Commission Earned!</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"A referred trader subscribed to <b>{html.escape(service_name)}</b>!\n\n"
                        f"• <b>Payment:</b> ${payment_amount} USD\n"
                        f"• <b>Your Tier Rate:</b> <b>{rate}%</b> ({paid_count} paid referrals){next_tier_text}\n"
                        f"• <b>Earned:</b> <b>+{share} USDT</b>\n"
                        f"• <b>Ref:</b> <code>{com_ref}</code>\n\n"
                        "Check your balance with /referral."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as exc:
                LOGGER.warning("Could not send referral commission alert to %s: %s", referrer_id, exc)
            return com_doc

    elif service == "private":
        try:
            inv_amt = submission.get("amount", "0")
            await context.bot.send_message(
                chat_id=referrer_id,
                text=(
                    "♟️ <b>Private Investment Referral Active!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"An investor you referred funded an investment of <b>${html.escape(str(inv_amt))} USDT</b>!\n\n"
                    "• <b>Program:</b> Private Investment\n"
                    f"• <b>Commission Structure:</b> {INVESTMENT_REFERRAL_RATE}% of realized referral profits\n"
                    f"• <b>Total Paid Referrals:</b> {paid_count}\n\n"
                    "Your profit commission will be credited as returns are realized."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            LOGGER.warning("Could not send private investment referral alert to %s: %s", referrer_id, exc)

    return None


async def complete_payment_verification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    submission: dict[str, Any],
    invite_link: str | None = None,
    orig_message: Any = None,
) -> bool:
    reference = submission["reference"]
    admin_user = update.effective_user
    db = get_db(context)
    now = utc_now()

    update_fields: dict[str, Any] = {
        "payment_status": "VERIFIED ✅",
        "onboarding_status": "Pending Details",
        "admin_verification_status": {
            "decision": "Approved",
            "reviewed_by": admin_user.id if admin_user else None,
            "reviewed_at": now,
        },
        "updated_at": now,
    }
    if invite_link:
        update_fields["invite_link"] = invite_link

    updated_sub = await db.submissions.find_one_and_update(
        {"reference": reference, "payment_status": "PENDING"},
        {"$set": update_fields},
        return_document=ReturnDocument.AFTER,
    )
    if not updated_sub:
        return False

    audit_entry: dict[str, Any] = {
        "action": "payment_verify",
        "reference": reference,
        "admin_id": admin_user.id if admin_user else None,
        "created_at": now,
    }
    if invite_link:
        audit_entry["invite_link"] = invite_link
    await db.audit.insert_one(audit_entry)

    # Edit review card in admin chat to reflect approval and remove action buttons
    rev_text = (
        admin_submission_text(updated_sub)
        + f"\n\n<b>Decision:</b> VERIFIED ✅\n"
        f"<b>Reviewed by:</b> {html.escape(admin_user.full_name if admin_user else 'Admin')}"
    )
    if invite_link:
        rev_text += f"\n<b>VIP Invite Link:</b> <code>{html.escape(invite_link)}</code>"

    target_msg = orig_message
    if not target_msg and update.callback_query and update.callback_query.message:
        target_msg = update.callback_query.message
    if target_msg:
        await edit_admin_review_message(target_msg, rev_text, reply_markup=None)

    service = updated_sub["service"]
    service_name = updated_sub.get("service_name", SERVICE_NAMES.get(service, service))

    if service == "private":
        user_message = (
            f"🎉 <b>Payment Status: VERIFIED ✅</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your investment payment of <b>${html.escape(str(updated_sub.get('amount', '')))} "
            f"{html.escape(updated_sub.get('currency', ''))}</b> for <b>{html.escape(service_name)}</b> "
            f"(Ref: <code>{reference}</code>) has been confirmed!\n\n"
            "👉 <b>Next Step — Complete Investor Onboarding:</b>\n"
            "Please tap below to confirm your investor details and receive your portal access."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Complete Onboarding", callback_data=f"onboard_start:{reference}")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
            ]
        )
    else:
        duration_key = updated_sub.get("duration_key", "1m")
        days = DURATION_DAYS.get(duration_key, 30)
        expires_at = now + timedelta(days=days)

        sub_doc = {
            "telegram_id": updated_sub["telegram_id"],
            "telegram_username": updated_sub.get("telegram_username"),
            "reference": reference,
            "service": service,
            "service_name": service_name,
            "track": updated_sub.get("track", "standard"),
            "duration_key": duration_key,
            "duration_days": days,
            "start_date": now,
            "expires_at": expires_at,
            "status": "active",
            "expiry_warning_sent": False,
            "created_at": now,
            "updated_at": now,
        }
        if invite_link:
            sub_doc["invite_link"] = invite_link

        await db.subscriptions.insert_one(sub_doc)
        await db.users.update_one(
            {"telegram_id": updated_sub["telegram_id"]},
            {
                "$set": {
                    f"subscriptions.{service}": {
                        "status": "active",
                        "expires_at": expires_at,
                        "reference": reference,
                        "duration_key": duration_key,
                        "invite_link": invite_link,
                    },
                    "updated_at": now,
                }
            },
        )

        target_channel_url = invite_link or await get_link(context, "pawns_channel")
        channel_buttons = []
        if is_http_url(target_channel_url):
            channel_buttons.append([InlineKeyboardButton("🚀 Join VIP Trading Channel", url=target_channel_url)])
        channel_buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")])

        dur_label = DURATION_LABELS.get(duration_key, duration_key)
        user_message = (
            f"🎉 <b>Payment Status: VERIFIED ✅</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your payment of <b>${html.escape(str(updated_sub.get('amount', '')))} "
            f"{html.escape(updated_sub.get('currency', ''))}</b> for <b>{html.escape(service_name)}</b> "
            f"(Ref: <code>{reference}</code>) has been confirmed!\n\n"
            f"<b>Subscription Period:</b> {html.escape(dur_label)} ({days} days)\n"
            f"<b>Expires On:</b> {expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "👉 <b>Join the VIP Channel:</b>\n"
            "Use the button below to join the private VIP channel and receive signals.\n"
            "⚠️ <i>Note: This invite link is single-use and assigned exclusively to your account. Do not share or forward it.</i>"
        )
        keyboard = InlineKeyboardMarkup(channel_buttons)

    await process_referral_on_payment_verified(context, updated_sub)

    try:
        await context.bot.send_message(
            chat_id=updated_sub["telegram_id"],
            text=user_message,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError as exc:
        LOGGER.warning("Could not notify user %s of payment review result: %s", updated_sub["telegram_id"], exc)

    return True


async def admin_review_verify_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    admin_user = update.effective_user
    if not is_admin_user(admin_user.id if admin_user else None, context):
        await query.answer("You are not authorised to perform this action.", show_alert=True)
        return ConversationHandler.END

    _, _, reference = query.data.split(":", 2)
    db = get_db(context)
    submission = await db.submissions.find_one({"reference": reference})
    if not submission:
        await query.answer("Submission not found.", show_alert=True)
        return ConversationHandler.END

    if submission.get("payment_status") != "PENDING":
        await query.answer(f"No change made. Current status: {submission.get('payment_status')}", show_alert=True)
        return ConversationHandler.END

    service = submission.get("service")
    if service == "private":
        await query.answer("Verifying private investment payment...")
        success = await complete_payment_verification(
            update=update,
            context=context,
            submission=submission,
            invite_link=None,
            orig_message=query.message,
        )
        if not success:
            await query.answer("Submission was already processed or is no longer pending.", show_alert=True)
        return ConversationHandler.END

    # For trading services (crypto, forex, synthetic), prompt admin for customer's one-time invite link
    await query.answer()
    service_name = submission.get("service_name", SERVICE_NAMES.get(service, service))
    username = submission.get("telegram_username")
    user_display = f"@{username}" if username else f"ID: {submission.get('telegram_id')}"
    duration_key = submission.get("duration_key", "1m")
    dur_label = DURATION_LABELS.get(duration_key, duration_key)
    amount = submission.get("amount", "")
    currency = submission.get("currency", "")

    prompt_text = (
        "🔗 <b>Enter VIP Channel Invite Link</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Reference:</b> <code>{reference}</code>\n"
        f"• <b>Subscriber:</b> {html.escape(user_display)}\n"
        f"• <b>Service:</b> {html.escape(service_name)} ({html.escape(dur_label)})\n"
        f"• <b>Amount Paid:</b> ${html.escape(str(amount))} {html.escape(currency)}\n\n"
        "Please reply with the <b>one-time private invite link</b> for this customer "
        "(e.g. <code>https://t.me/+AbCdEf12345</code>).\n\n"
        "Once entered, payment is verified and the user will receive the <b>[🚀 Join VIP Trading Channel]</b> button with this link."
    )
    prompt_markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚡ Use Default Configured Channel Link", callback_data=f"admin:verify_default:{reference}")],
            [InlineKeyboardButton("❌ Cancel Verification", callback_data=f"admin:verify_cancel:{reference}")],
        ]
    )
    prompt_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=prompt_text,
        parse_mode=ParseMode.HTML,
        reply_markup=prompt_markup,
    )
    context.user_data["admin_verify"] = {
        "reference": reference,
        "orig_message": query.message,
        "prompt_msg_id": prompt_msg.message_id,
    }
    return ADMIN_VERIFY_LINK_INPUT


async def receive_admin_verify_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    verify_data = context.user_data.get("admin_verify")
    if not verify_data:
        return ConversationHandler.END

    raw_text = update.message.text.strip()
    norm_link = normalize_invite_link(raw_text)
    reference = verify_data.get("reference")

    if not norm_link:
        await update.message.reply_text(
            "⚠️ That does not appear to be a valid Telegram channel invite link.\n\n"
            "Please paste a valid link (e.g. <code>https://t.me/+AbCdEf12345</code>), "
            "or tap <b>Cancel Verification</b> below.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⚡ Use Default Configured Channel Link", callback_data=f"admin:verify_default:{reference}")],
                    [InlineKeyboardButton("❌ Cancel Verification", callback_data=f"admin:verify_cancel:{reference}")],
                ]
            ),
        )
        return ADMIN_VERIFY_LINK_INPUT

    db = get_db(context)
    submission = await db.submissions.find_one({"reference": reference})
    if not submission:
        await update.message.reply_text("❌ Submission not found in database.")
        context.user_data.pop("admin_verify", None)
        return ConversationHandler.END

    orig_message = verify_data.get("orig_message")
    prompt_msg_id = verify_data.get("prompt_msg_id")
    if prompt_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
        except Exception:
            pass

    success = await complete_payment_verification(
        update=update,
        context=context,
        submission=submission,
        invite_link=norm_link,
        orig_message=orig_message,
    )
    context.user_data.pop("admin_verify", None)

    if success:
        service_name = submission.get("service_name", SERVICE_NAMES.get(submission.get("service"), ""))
        await update.message.reply_text(
            "✅ <b>Payment Verified & Invite Link Sent!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Reference:</b> <code>{reference}</code>\n"
            f"• <b>Service:</b> {html.escape(service_name)}\n"
            f"• <b>Delivered Link:</b> <code>{html.escape(norm_link)}</code>\n\n"
            "The customer has received their confirmation with the VIP channel button.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"⚠️ Submission <code>{reference}</code> was already processed or is no longer pending.",
            parse_mode=ParseMode.HTML,
        )
    return ConversationHandler.END


async def receive_admin_verify_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    reference = query.data.split(":", 2)[2]
    verify_data = context.user_data.get("admin_verify", {})
    orig_message = verify_data.get("orig_message")

    db = get_db(context)
    submission = await db.submissions.find_one({"reference": reference})
    if not submission:
        await query.edit_message_text("❌ Submission not found.")
        context.user_data.pop("admin_verify", None)
        return ConversationHandler.END

    default_channel = await get_link(context, "pawns_channel")
    success = await complete_payment_verification(
        update=update,
        context=context,
        submission=submission,
        invite_link=default_channel,
        orig_message=orig_message,
    )
    context.user_data.pop("admin_verify", None)

    if success:
        await query.edit_message_text(
            "✅ <b>Payment Verified with Default Channel Link</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Reference:</b> <code>{reference}</code>\n"
            f"• <b>Channel Link Delivered:</b> <code>{html.escape(default_channel or 'None')}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.edit_message_text(
            f"⚠️ Submission <code>{reference}</code> was already processed or is no longer pending.",
            parse_mode=ParseMode.HTML,
        )
    return ConversationHandler.END


async def receive_admin_verify_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Verification cancelled.")
    context.user_data.pop("admin_verify", None)
    await query.edit_message_text(
        "❌ <b>Verification Cancelled</b>\n"
        "The submission remains in PENDING status.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def admin_verify_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("admin_verify", None)
    await update.message.reply_text(
        "❌ Verification cancelled. The payment submission remains in PENDING status."
    )
    return ConversationHandler.END


async def admin_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings = get_settings(context)
    admin_user = update.effective_user
    if not is_admin_user(admin_user.id if admin_user else None, context):
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

    if action == "verify":
        submission = await db.submissions.find_one({"reference": reference, "payment_status": "PENDING"})
        if not submission:
            current = await db.submissions.find_one({"reference": reference})
            curr_status = current.get("payment_status", "not found") if current else "not found"
            await query.answer(f"No change made. Current status: {curr_status}", show_alert=True)
            return
        await query.answer("Payment verified.")
        await complete_payment_verification(
            update=update,
            context=context,
            submission=submission,
            invite_link=None,
            orig_message=query.message,
        )
        return

    # action == "reject"
    submission = await db.submissions.find_one_and_update(
        {"reference": reference, "payment_status": "PENDING"},
        {
            "$set": {
                "payment_status": "REJECTED",
                "onboarding_status": "Rejected",
                "admin_verification_status": {
                    "decision": "Rejected",
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
            "action": "payment_reject",
            "reference": reference,
            "admin_id": admin_user.id,
            "created_at": now,
        }
    )
    await query.answer("Payment marked REJECTED.")

    rev_text = (
        admin_submission_text(submission)
        + f"\n\n<b>Decision:</b> REJECTED\n"
        f"<b>Reviewed by:</b> {html.escape(admin_user.full_name)}"
    )
    await edit_admin_review_message(query.message, rev_text, reply_markup=None)

    service = submission["service"]
    service_name = submission.get("service_name", SERVICE_NAMES.get(service, service))
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
        "private": "Confirm your name or any onboarding notes to activate your portfolio:",
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

    if updated.get("service") == "private":
        await db.users.update_one(
            {"telegram_id": update.effective_user.id},
            {
                "$set": {
                    "investor_status": "active",
                    "investor_details": {
                        "full_name": updated.get("full_name"),
                        "reference": ref,
                        "investment_amount": updated.get("selected_plan", {}).get("investment_amount") or updated.get("amount"),
                        "risk_category": updated.get("selected_plan", {}).get("risk_category"),
                        "duration": updated.get("selected_plan", {}).get("duration"),
                        "proposed_return": updated.get("selected_plan", {}).get("proposed_return"),
                        "onboarded_at": now,
                    },
                    "updated_at": now,
                }
            },
        )
        pawns_channel = await get_link(context, "pawns_channel")
        portal_bot = await get_link(context, "investor_portal_bot")
        links_buttons = []
        if is_http_url(pawns_channel):
            links_buttons.append([InlineKeyboardButton("📢 PAWNS Community Channel", url=pawns_channel)])
        if is_http_url(portal_bot):
            links_buttons.append([InlineKeyboardButton("🤖 PAWNS Investor Portal Bot", url=portal_bot)])
        links_buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")])

        await message.reply_text(
            "🎉 <b>Onboarding Complete & Portfolio Activated!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your private investment portfolio for <b>Ref: <code>{ref}</code></b> is now officially active!\n\n"
            "🔗 <b>Access Your Channels & Portals:</b>\n"
            "Join our official investor community and access your dedicated investor bot below.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(links_buttons),
        )
    else:
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


async def cmd_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_adm = is_admin_user(user.id, context)
    role_str = "👑 <b>Authorized Administrator ✅</b>" if is_adm else "👤 <b>Standard User</b>"
    text = (
        "🆔 <b>YOUR TELEGRAM IDENTITY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Telegram ID:</b> <code>{user.id}</code>\n"
        f"• <b>Full Name:</b> {html.escape(user.full_name)}\n"
        f"• <b>Username:</b> @{html.escape(user.username or 'None')}\n"
        f"• <b>Status:</b> {role_str}\n\n"
    )
    if not is_adm:
        text += (
            "<i>If you are an administrator, copy your Telegram ID above and add it to "
            "<code>ADMIN_CHAT_IDS</code> in your environment, or ask an existing administrator to run "
            f"<code>/addadmin {user.id}</code>.</i>"
        )
    else:
        text += "<i>You have active administrator privileges. Use /admin to access the control panel.</i>"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def admin_panel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Platform Stats", callback_data="admin:stats"),
                InlineKeyboardButton("⏳ Pending Reviews", callback_data="admin:pending"),
            ],
            [
                InlineKeyboardButton("⚙️ System Settings", callback_data="admin:settings_view"),
                InlineKeyboardButton("👥 Manage Admins", callback_data="admin:manage"),
            ],
            [
                InlineKeyboardButton("📢 New Announcement", callback_data="admin:announce"),
                InlineKeyboardButton("📋 Recent Audit Log", callback_data="admin:audit"),
            ],
            [
                InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="menu"),
            ],
        ]
    )
    text = (
        "🛠 <b>PAWNS ADMIN CONTROL CENTER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Welcome, Administrator! Select an operation below or send an administrative command.\n\n"
        "<b>Available Admin Commands:</b>\n"
        "• <code>/announcement</code> — Broadcast announcement to all users\n"
        "• <code>/stats</code> — View live platform statistics\n"
        "• <code>/admins</code> — List &amp; view authorized admins\n"
        "• <code>/addadmin &lt;id&gt;</code> — Grant admin role\n"
        "• <code>/removeadmin &lt;id&gt;</code> — Revoke admin role\n"
        "• <code>/setwallet &lt;type&gt; &lt;addr&gt;</code> — Update payment wallet\n"
        "• <code>/setfee &lt;service&gt; &lt;amt&gt;</code> — Update pricing\n"
        "• <code>/setnairarate &lt;rate&gt;</code> — Update USD/NGN rate\n"
        "• <code>/checkexpiry</code> — Check subscription expiries\n"
        "• <code>/audit</code> — Inspect audit trail\n"
        "• <code>/report</code> — Generate CSV export report"
    )
    if update.callback_query:
        await send_or_edit(update, text, keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def admin_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    db = get_db(context)
    pending_submissions = await db.submissions.find(
        {"payment_status": {"$in": ["PENDING", "Under Review"]}}
    ).sort("created_at", -1).to_list(length=10)

    pending_count = await db.submissions.count_documents(
        {"payment_status": {"$in": ["PENDING", "Under Review"]}}
    )

    text = (
        "⏳ <b>PENDING SUBMISSIONS QUEUE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Total Submissions Awaiting Review:</b> {pending_count}\n\n"
    )
    if not pending_submissions:
        text += "🎉 <i>No submissions currently awaiting review. All caught up!</i>"
    else:
        for sub in pending_submissions:
            ref = sub.get("reference", "N/A")
            svc = sub.get("service_name", sub.get("service", "Trading"))
            amt = sub.get("amount", "0")
            cur = sub.get("currency", "USDT")
            uname = sub.get("telegram_username")
            u_str = f"@{uname}" if uname else f"ID: {sub.get('telegram_id')}"
            text += f"• <code>{ref}</code> | {svc} | ${amt} {cur} | {u_str}\n"
        text += "\n<i>Review submissions via notifications or manual review commands.</i>"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin:menu")]
    ])
    await send_or_edit(update, text, keyboard)


async def admin_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    settings = get_settings(context)
    db = get_db(context)
    env_admins = set(settings.admin_chat_ids)

    db_admins = []
    async for doc in db.users.find({"is_admin": True}):
        tid = doc.get("telegram_id")
        uname = doc.get("telegram_username")
        fname = doc.get("full_name") or ""
        db_admins.append((tid, uname, fname))

    text = (
        "👥 <b>ADMINISTRATOR ROLES &amp; ACCESS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Bootstrap Admins (from ADMIN_CHAT_IDS env):</b>\n"
    )
    for tid in env_admins:
        text += f"• <code>{tid}</code> (Configured in Environment)\n"

    text += "\n<b>Database Admins (MongoDB):</b>\n"
    extra_count = 0
    for tid, uname, fname in db_admins:
        if tid not in env_admins:
            extra_count += 1
            uname_str = f"@{uname}" if uname else fname or "No username"
            text += f"• <code>{tid}</code> — {html.escape(uname_str)}\n"
    if extra_count == 0:
        text += "<i>No additional database admins.</i>\n"

    text += (
        "\n<b>Management Commands:</b>\n"
        "• <code>/addadmin &lt;telegram_id&gt;</code> — Grant admin privileges\n"
        "• <code>/removeadmin &lt;telegram_id&gt;</code> — Revoke admin privileges"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin:menu")]
    ])
    if update.callback_query:
        await send_or_edit(update, text, keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def admin_audit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    db = get_db(context)
    events = await db.audit.find().sort("created_at", -1).to_list(length=10)
    text = (
        "📋 <b>RECENT AUDIT TRAIL (Last 10 Events)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    if not events:
        text += "<i>No audit events recorded yet.</i>"
    else:
        for ev in events:
            action = ev.get("action", "unknown")
            admin_id = ev.get("admin_id", "System")
            dt = ev.get("created_at")
            dt_str = dt.strftime("%m-%d %H:%M") if isinstance(dt, datetime) else str(dt)[:16]
            details = ev.get("details", {})
            ref = details.get("reference", "") if isinstance(details, dict) else ev.get("reference", "")
            ref_str = f" [<code>{ref}</code>]" if ref else ""
            text += f"• <code>{dt_str}</code> | <b>{html.escape(action)}</b> by <code>{admin_id}</code>{ref_str}\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin:menu")]
    ])
    await send_or_edit(update, text, keyboard)


async def admin_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: <code>/addadmin &lt;telegram_id&gt;</code>\n"
            "Example: <code>/addadmin 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        target_id = int(context.args[0].strip())
    except ValueError:
        await update.effective_message.reply_text("❌ Telegram ID must be a numeric integer.")
        return
    db = get_db(context)
    now = utc_now()
    await db.users.update_one(
        {"telegram_id": target_id},
        {"$set": {"is_admin": True, "updated_at": now}},
        upsert=True,
    )
    context.application.bot_data.setdefault("admin_ids", set()).add(target_id)
    try:
        set_cmd = getattr(context.bot, "set_my_commands", None)
        if callable(set_cmd):
            res = set_cmd(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=target_id))
            if asyncio.iscoroutine(res):
                await res
    except Exception as exc:
        LOGGER.warning("Could not set admin commands for new admin %s: %s", target_id, exc)
    await db.audit.insert_one({
        "action": "admin_added",
        "target_id": target_id,
        "admin_id": update.effective_user.id,
        "created_at": now,
    })
    await update.effective_message.reply_text(
        f"✅ <b>Admin Added Successfully</b>\n\nTelegram ID <code>{target_id}</code> now has administrative privileges.",
        parse_mode=ParseMode.HTML,
    )


async def admin_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: <code>/removeadmin &lt;telegram_id&gt;</code>\n"
            "Example: <code>/removeadmin 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        target_id = int(context.args[0].strip())
    except ValueError:
        await update.effective_message.reply_text("❌ Telegram ID must be a numeric integer.")
        return
    settings = get_settings(context)
    if target_id in settings.admin_chat_ids:
        await update.effective_message.reply_text(
            f"⚠️ Cannot revoke Telegram ID <code>{target_id}</code> because it is configured in the environment <code>ADMIN_CHAT_IDS</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    db = get_db(context)
    now = utc_now()
    await db.users.update_one(
        {"telegram_id": target_id},
        {"$set": {"is_admin": False, "updated_at": now}},
    )
    admin_set = context.application.bot_data.get("admin_ids")
    if admin_set and target_id in admin_set:
        admin_set.discard(target_id)
    try:
        del_cmd = getattr(context.bot, "delete_my_commands", None)
        if callable(del_cmd):
            res = del_cmd(scope=BotCommandScopeChat(chat_id=target_id))
            if asyncio.iscoroutine(res):
                await res
    except Exception as exc:
        LOGGER.warning("Could not delete admin commands for revoked admin %s: %s", target_id, exc)
    await db.audit.insert_one({
        "action": "admin_removed",
        "target_id": target_id,
        "admin_id": update.effective_user.id,
        "created_at": now,
    })
    await update.effective_message.reply_text(
        f"✅ <b>Admin Removed</b>\n\nTelegram ID <code>{target_id}</code> no longer has administrative privileges.",
        parse_mode=ParseMode.HTML,
    )


async def admin_list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    await admin_manage_callback(update, context)


def format_announcement_message(body: str) -> str:
    return (
        "🔊 <b>PAWNS ANNOUNCEMENT</b> 🔊\n"
        "──────────────────────────\n"
        f"{body}\n"
        "──────────────────────────"
    )


async def admin_announcement_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await require_admin(update, context):
        return ConversationHandler.END

    context.user_data.pop("announcement_content", None)
    db = get_db(context)
    user_count = await db.users.count_documents({})

    text = (
        "📢 <b>PAWNS BROADCAST ANNOUNCEMENT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"You are preparing an official broadcast for <b>{user_count}</b> registered bot user(s).\n\n"
        "✍️ <b>Please send your announcement message text below:</b>\n"
        "<i>(You will be shown a structured preview before anything is broadcasted)</i>\n\n"
        "Send /cancel to abort at any time."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="announce:cancel")]
    ])
    if update.callback_query:
        await update.callback_query.answer()
        await send_or_edit(update, text, keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return ANNOUNCEMENT_TEXT_INPUT


async def receive_announcement_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await require_admin(update, context):
        return ConversationHandler.END

    message = update.effective_message
    if not message:
        return ANNOUNCEMENT_TEXT_INPUT

    raw_text = (message.text_html or (html.escape(message.text) if message.text else "")).strip()
    if not raw_text:
        await message.reply_text("❌ Announcement text cannot be empty. Please send your message or /cancel:")
        return ANNOUNCEMENT_TEXT_INPUT

    context.user_data["announcement_content"] = raw_text

    db = get_db(context)
    user_count = await db.users.count_documents({})

    formatted_msg = format_announcement_message(raw_text)

    preview_text = (
        "📢 <b>ANNOUNCEMENT PREVIEW</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Below is how your announcement will appear to users:</i>\n\n"
        f"{formatted_msg}\n\n"
        f"👥 <b>Target Audience:</b> {user_count} registered user(s)\n\n"
        "Do you want to proceed and broadcast this announcement now?"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Proceed & Broadcast", callback_data="announce:proceed"),
            InlineKeyboardButton("❌ Cancel", callback_data="announce:cancel"),
        ]
    ])

    await message.reply_text(
        preview_text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return ANNOUNCEMENT_CONFIRM


async def admin_announcement_proceed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    if not await require_admin(update, context):
        return ConversationHandler.END

    content = context.user_data.pop("announcement_content", None)
    if not content:
        if query:
            await query.edit_message_text("⚠️ Announcement session expired. Run /announcement to start again.")
        return ConversationHandler.END

    if query:
        await query.edit_message_text(
            "⏳ <b>Broadcasting in progress...</b>\n\n"
            "Please wait while the announcement is delivered to all registered users.",
            parse_mode=ParseMode.HTML,
        )

    db = get_db(context)
    bot = context.bot

    user_ids: set[int] = set()
    cursor = db.users.find({}, {"telegram_id": 1})
    async for doc in cursor:
        tid = doc.get("telegram_id")
        if tid and isinstance(tid, int):
            user_ids.add(tid)

    broadcast_msg = format_announcement_message(content)

    sent_count = 0
    failed_count = 0

    for tid in user_ids:
        try:
            await bot.send_message(
                chat_id=tid,
                text=broadcast_msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent_count += 1
            await asyncio.sleep(0.04)  # ~25 msg/sec rate-limit safeguard
        except (Forbidden, BadRequest, TelegramError) as exc:
            LOGGER.warning("Failed to deliver broadcast to user %s: %s", tid, exc)
            failed_count += 1
        except Exception as exc:
            LOGGER.error("Unexpected error delivering broadcast to %s: %s", tid, exc)
            failed_count += 1

    now = utc_now()
    admin_id = update.effective_user.id if update.effective_user else "unknown"
    await db.audit.insert_one({
        "action": "announcement_broadcast",
        "admin_id": admin_id,
        "content_length": len(content),
        "total_targets": len(user_ids),
        "sent_count": sent_count,
        "failed_count": failed_count,
        "created_at": now,
    })

    admin_username = update.effective_user.username if update.effective_user else None
    sender_str = f"@{admin_username}" if admin_username else f"ID {admin_id}"

    result_text = (
        "✅ <b>ANNOUNCEMENT BROADCAST COMPLETED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Successfully Delivered:</b> {sent_count} user(s)\n"
        f"• <b>Failed / Blocked:</b> {failed_count} user(s)\n"
        f"• <b>Total Audience:</b> {len(user_ids)} registered\n"
        f"• <b>Broadcasted By:</b> {sender_str}\n"
        f"• <b>Timestamp:</b> {now.strftime('%Y-%m-%d %H:%M UTC')}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin:menu")]
    ])

    if query:
        await query.edit_message_text(result_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif update.effective_message:
        await update.effective_message.reply_text(result_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    return ConversationHandler.END


async def admin_announcement_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data.pop("announcement_content", None)
    text = "❌ <b>Announcement cancelled.</b> No messages were broadcasted."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin:menu")]
    ])
    if query:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    db = get_db(context)
    users = await db.users.count_documents({})
    paid_referrals = await db.users.count_documents({"is_paid_referral": True})
    pending = await db.submissions.count_documents({"payment_status": {"$in": ["PENDING", "Under Review"]}})
    verified = await db.submissions.count_documents({"payment_status": {"$in": ["VERIFIED ✅", "Verified"]}})
    rejected = await db.submissions.count_documents({"payment_status": {"$in": ["REJECTED", "Rejected"]}})
    text = (
        "📊 <b>PAWNS ADMIN STATS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Registered Users:</b> {users}\n"
        f"• <b>Verified Paid Referrals:</b> {paid_referrals}\n"
        f"• <b>Pending Review Payments:</b> {pending}\n"
        f"• <b>Verified Payments:</b> {verified}\n"
        f"• <b>Rejected Payments:</b> {rejected}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin:menu")]
    ]) if update.callback_query else None
    if update.callback_query:
        await send_or_edit(update, text, keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def admin_settings_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    settings = get_settings(context)
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
    usd_rate = await db.get_setting("usd_ngn_rate", str(settings.usd_ngn_rate))

    text = (
        "⚙️ <b>PAWNS ADMIN CONFIGURATION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Investment Wallet ({inv_net}):</b>\n<code>{inv_wallet}</code>\n\n"
        f"<b>Trading Services Wallet ({trd_net}):</b>\n<code>{trd_wallet}</code>\n\n"
        "<b>Naira Bank Details (Forex Only):</b>\n"
        f"• Bank: {html.escape(settings.naira_bank_name)}\n"
        f"• Account Number: <code>{html.escape(settings.naira_account_number)}</code>\n"
        f"• Account Name: {html.escape(settings.naira_account_name)}\n"
        f"• USD/NGN Rate: <b>₦{usd_rate}</b> / $1 USD\n\n"
        "<b>Service Fees:</b>\n"
        f"• Crypto Futures: ${fee_crypto}\n"
        f"• Forex Live: ${fee_forex_live}\n"
        f"• Forex Prop: ${fee_forex_prop}\n"
        f"• Synthetic Trading: ${fee_synthetic}\n"
        f"• Minimum Investment: ${min_invest}\n\n"
        "<b>Payment Instructions:</b>\n"
        f"• Investment: {inst_inv}\n"
        f"• Trading: {inst_trd}\n\n"
        "<b>Partner Brokers:</b>\n"
        f"• {html.escape(settings.broker_1_name)} | {html.escape(settings.broker_2_name)}\n\n"
        "<b>Partner Prop Firms:</b>\n"
        f"• {html.escape(settings.prop_1_name)} | {html.escape(settings.prop_2_name)}\n\n"
        "<b>Admin Commands:</b>\n"
        "• <code>/setwallet &lt;investment|trading&gt; &lt;address&gt;</code>\n"
        "• <code>/setfee &lt;crypto|forex_live|forex_prop|synthetic&gt; &lt;amount&gt;</code>\n"
        "• <code>/setmininvest &lt;amount&gt;</code>\n"
        "• <code>/setnetwork &lt;investment|trading&gt; &lt;network&gt;</code>\n"
        "• <code>/setinstructions &lt;investment|trading&gt; &lt;text&gt;</code>\n"
        "• <code>/setnairarate &lt;rate&gt;</code>\n"
        "• <code>/addadmin &lt;telegram_id&gt;</code>\n"
        "• <code>/removeadmin &lt;telegram_id&gt;</code>\n"
        "• <code>/checkexpiry</code>\n"
        "• <code>/audit</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin:menu")]
    ]) if update.callback_query else None
    if update.callback_query:
        await send_or_edit(update, text, keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    await admin_settings_view(update, context)


async def admin_set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    settings = get_settings(context)
    admin_id = update.effective_user.id if update.effective_user else 0

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
    if not await require_admin(update, context):
        return
    admin_id = update.effective_user.id if update.effective_user else 0

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
    if not await require_admin(update, context):
        return
    admin_id = update.effective_user.id if update.effective_user else 0

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
    if not await require_admin(update, context):
        return
    admin_id = update.effective_user.id if update.effective_user else 0

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
    if not await require_admin(update, context):
        return
    admin_id = update.effective_user.id if update.effective_user else 0

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


async def admin_set_naira_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    admin_id = update.effective_user.id if update.effective_user else 0

    if not context.args or len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /setnairarate &lt;rate&gt;\nExample: <code>/setnairarate 1450</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    rate_str = context.args[0].strip().replace(",", "")
    try:
        rate = Decimal(rate_str)
        if rate <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Rate must be a positive number.")
        return

    db = get_db(context)
    audit_rec = await db.set_setting("usd_ngn_rate", str(rate), admin_id)
    await update.effective_message.reply_text(
        f"✅ Updated <b>USD to NGN Exchange Rate</b> to <b>₦{rate}</b> (was ₦{audit_rec.get('old_value', '')}).\n"
        "New payments will immediately use this exchange rate.",
        parse_mode=ParseMode.HTML,
    )


async def run_subscription_expiry_check(application: Application) -> tuple[int, int]:
    db: Database = application.bot_data["db"]
    settings: Settings = application.bot_data["settings"]
    now = utc_now()
    four_days_from_now = now + timedelta(days=4)
    warned_count = 0
    expired_count = 0

    # 1. Subscriptions expiring within 4 days (not yet warned)
    near_cursor = db.subscriptions.find({
        "status": "active",
        "expiry_warning_sent": {"$ne": True},
        "expires_at": {"$lte": four_days_from_now, "$gt": now},
    })
    async for sub in near_cursor:
        delta = sub["expires_at"] - now
        days_left = max(1, delta.days)
        time_str = f"{days_left} day(s)" if days_left > 1 else f"{max(1, delta.seconds // 3600)} hour(s)"
        svc_name = sub.get("service_name", sub.get("service", "Trading Service"))

        sub_id = sub.get("_id")
        if sub_id:
            await db.subscriptions.update_one(
                {"_id": sub_id},
                {"$set": {"expiry_warning_sent": True, "updated_at": now}},
            )
        else:
            await db.subscriptions.update_one(
                {"reference": sub.get("reference")},
                {"$set": {"expiry_warning_sent": True, "updated_at": now}},
            )
        warned_count += 1

        svc = sub.get("service")
        if svc == "crypto":
            track = sub.get("track", "standard")
            renew_cb = f"cf_pay:{track}:1m"
        elif svc in {"forex_live", "forex_prop"}:
            forex_type = "live" if svc == "forex_live" else "prop"
            renew_cb = f"forex_pay:{forex_type}:1m"
        else:
            renew_cb = "menu"

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Renew Subscription", callback_data=renew_cb)],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
            ]
        )
        user_msg = (
            "⚠️ <b>SUBSCRIPTION EXPIRING SOON</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your access to <b>{html.escape(svc_name)}</b> will expire in approximately <b>{time_str}</b> "
            f"({sub['expires_at'].strftime('%Y-%m-%d %H:%M UTC')}).\n\n"
            "To maintain uninterrupted access to PAWNS VIP signals and channels, please renew your subscription."
        )
        try:
            await application.bot.send_message(
                chat_id=sub["telegram_id"],
                text=user_msg,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramError as exc:
            LOGGER.warning("Could not send expiry warning to user %s: %s", sub["telegram_id"], exc)

        admin_msg = (
            "⚠️ <b>UPCOMING SUBSCRIPTION EXPIRY NOTICE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>User:</b> @{html.escape(sub.get('telegram_username') or 'N/A')}\n"
            f"<b>Telegram ID:</b> <code>{sub['telegram_id']}</code>\n"
            f"<b>Service:</b> {html.escape(svc_name)}\n"
            f"<b>Expires in:</b> {time_str} ({sub['expires_at'].strftime('%Y-%m-%d %H:%M UTC')})\n\n"
            "<i>Reminder: Access will need to be removed from VIP channel if not renewed upon expiration.</i>"
        )
        for admin_id in settings.admin_chat_ids:
            try:
                await application.bot.send_message(
                    chat_id=admin_id,
                    text=admin_msg,
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass

    # 2. Subscriptions expired (expires_at <= now, marked active)
    expired_cursor = db.subscriptions.find({
        "status": "active",
        "expires_at": {"$lte": now},
    })
    async for sub in expired_cursor:
        svc_name = sub.get("service_name", sub.get("service", "Trading Service"))
        svc = sub.get("service")
        sub_id = sub.get("_id")
        if sub_id:
            await db.subscriptions.update_one(
                {"_id": sub_id},
                {"$set": {"status": "expired", "updated_at": now}},
            )
        else:
            await db.subscriptions.update_one(
                {"reference": sub.get("reference")},
                {"$set": {"status": "expired", "updated_at": now}},
            )

        if svc:
            await db.users.update_one(
                {"telegram_id": sub["telegram_id"]},
                {"$set": {f"subscriptions.{svc}.status": "expired", "updated_at": now}},
            )
        expired_count += 1

        if svc == "crypto":
            track = sub.get("track", "standard")
            renew_cb = f"cf_pay:{track}:1m"
        elif svc in {"forex_live", "forex_prop"}:
            forex_type = "live" if svc == "forex_live" else "prop"
            renew_cb = f"forex_pay:{forex_type}:1m"
        else:
            renew_cb = "menu"

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Renew Subscription", callback_data=renew_cb)],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
            ]
        )
        user_msg = (
            "🔴 <b>SUBSCRIPTION EXPIRED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your access to <b>{html.escape(svc_name)}</b> has expired.\n\n"
            "To regain access to PAWNS VIP signals and channels, please renew your subscription below."
        )
        try:
            await application.bot.send_message(
                chat_id=sub["telegram_id"],
                text=user_msg,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramError as exc:
            LOGGER.warning("Could not send expired notice to user %s: %s", sub["telegram_id"], exc)

        admin_msg = (
            "🚨 <b>SUBSCRIPTION EXPIRED — ACTION REQUIRED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>User:</b> @{html.escape(sub.get('telegram_username') or 'N/A')}\n"
            f"<b>Telegram ID:</b> <code>{sub['telegram_id']}</code>\n"
            f"<b>Service:</b> {html.escape(svc_name)}\n"
            f"<b>Expired at:</b> {sub['expires_at'].strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "⚠️ <b>Reminder to Admin:</b> Please remove this user from the private VIP channel / group if they do not renew."
        )
        for admin_id in settings.admin_chat_ids:
            try:
                await application.bot.send_message(
                    chat_id=admin_id,
                    text=admin_msg,
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass

    return warned_count, expired_count


async def subscription_expiry_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.info("Running scheduled 4-day subscription expiry check...")
    try:
        warned, expired = await run_subscription_expiry_check(context.application)
        LOGGER.info("Subscription expiry check complete. Warnings: %d, Expired: %d", warned, expired)
    except Exception as exc:
        LOGGER.exception("Error during subscription expiry check: %s", exc)


async def admin_check_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    await update.effective_message.reply_text("⏳ Running subscription expiry check...")
    warned, expired = await run_subscription_expiry_check(context.application)
    await update.effective_message.reply_text(
        f"✅ <b>Expiry Check Completed</b>\n\n"
        f"• Expiry warnings sent: <b>{warned}</b>\n"
        f"• Subscriptions marked expired: <b>{expired}</b>",
        parse_mode=ParseMode.HTML,
    )


# --- BingX UID Flow ---
async def bingx_uid_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🆔 <b>Submit Your BingX UID</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Please enter your numeric BingX User ID (UID):\n\n"
        "<i>(You can find your UID in your BingX Profile)</i>\n\n"
        "Send /cancel to return to the menu.",
        parse_mode=ParseMode.HTML,
    )
    return BINGX_UID_INPUT


async def receive_bingx_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    raw_uid = (message.text or "").strip()
    if FORBIDDEN_SECRET_RE.search(raw_uid):
        await message.reply_text("⚠️ Do not submit passwords or private keys. Please submit only your BingX UID.")
        return BINGX_UID_INPUT

    if not raw_uid.isalnum() or len(raw_uid) < 4 or len(raw_uid) > 30:
        await message.reply_text("❌ Please enter a valid BingX UID (numbers/letters, 4-30 characters).")
        return BINGX_UID_INPUT

    user = update.effective_user
    db = get_db(context)
    now = utc_now()

    record = {
        "telegram_id": user.id,
        "telegram_username": user.username,
        "full_name": user.full_name,
        "uid": raw_uid,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    await db.bingx_verifications.insert_one(record)

    await message.reply_text(
        "⏳ <b>BingX UID Submitted</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Your BingX UID <code>{html.escape(raw_uid)}</code> has been submitted to the verification desk.\n\n"
        "Once verified by an administrator, you will receive a notification and unlock discounted subscription rates.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )

    settings = get_settings(context)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"admin:bingx_approve:{user.id}:{raw_uid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"admin:bingx_reject:{user.id}:{raw_uid}"),
            ]
        ]
    )
    admin_text = (
        "🆔 <b>NEW BINGX UID VERIFICATION REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>User:</b> {html.escape(user.full_name)} (@{html.escape(user.username or 'N/A')})\n"
        f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
        f"<b>BingX UID:</b> <code>{html.escape(raw_uid)}</code>"
    )
    for admin_id in settings.admin_chat_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramError as exc:
            LOGGER.error("Could not notify admin %s of BingX UID: %s", admin_id, exc)

    return ConversationHandler.END


async def admin_bingx_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings = get_settings(context)
    admin_user = update.effective_user
    if not is_admin_user(admin_user.id if admin_user else None, context):
        await query.answer("You are not authorised to perform this action.", show_alert=True)
        return

    parts = query.data.split(":", 3)
    action = parts[1]
    target_user_id = int(parts[2])
    raw_uid = parts[3]
    db = get_db(context)
    now = utc_now()

    if action == "bingx_approve":
        await db.bingx_verifications.update_one(
            {"telegram_id": target_user_id, "uid": raw_uid},
            {"$set": {"status": "approved", "reviewed_by": admin_user.id, "updated_at": now}},
        )
        await db.users.update_one(
            {"telegram_id": target_user_id},
            {"$set": {"bingx_verified": True, "bingx_uid": raw_uid, "updated_at": now}},
        )
        await query.answer("BingX UID approved.")
        try:
            await query.edit_message_text(
                query.message.text_html + f"\n\n<b>Decision:</b> APPROVED ✅ by {html.escape(admin_user.full_name)}",
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass

        buttons = [
            [InlineKeyboardButton("1 Month — $40", callback_data="cf_pay:bingx:1m")],
            [InlineKeyboardButton("3 Months — $90", callback_data="cf_pay:bingx:3m")],
            [InlineKeyboardButton("6 Months — $200", callback_data="cf_pay:bingx:6m")],
            [InlineKeyboardButton("1 Year — $300", callback_data="cf_pay:bingx:12m")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
        ]
        user_msg = (
            "🎉 <b>BingX UID Verified!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your BingX UID <code>{html.escape(raw_uid)}</code> has been verified.\n\n"
            "You now qualify for discounted PAWNS Crypto Futures subscriptions! Choose your duration below to proceed to payment:"
        )
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=user_msg,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except TelegramError as exc:
            LOGGER.warning("Could not send BingX approval to user %s: %s", target_user_id, exc)
    else:
        await db.bingx_verifications.update_one(
            {"telegram_id": target_user_id, "uid": raw_uid},
            {"$set": {"status": "rejected", "reviewed_by": admin_user.id, "updated_at": now}},
        )
        await query.answer("BingX UID rejected.")
        try:
            await query.edit_message_text(
                query.message.text_html + f"\n\n<b>Decision:</b> REJECTED ❌ by {html.escape(admin_user.full_name)}",
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass

        support_url = await get_link(context, "support")
        buttons = []
        if is_http_url(support_url):
            buttons.append([InlineKeyboardButton("🛟 Contact Support", url=support_url)])
        buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")])
        user_msg = (
            "❌ <b>BingX UID Verification Failed</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your BingX UID <code>{html.escape(raw_uid)}</code> could not be verified under the PAWNS affiliate desk.\n\n"
            "Please ensure you registered using our official partner link, or contact support for assistance."
        )
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=user_msg,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except TelegramError as exc:
            LOGGER.warning("Could not send BingX rejection to user %s: %s", target_user_id, exc)


# --- Investor Hub Handlers ---
async def investor_report_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    db = get_db(context)
    now = utc_now()

    ref = make_reference("REP")
    record = {
        "reference": ref,
        "telegram_id": user.id,
        "telegram_username": user.username,
        "full_name": user.full_name,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    await db.reports.insert_one(record)

    await query.edit_message_text(
        "📊 <b>Portfolio Report Request Submitted</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{ref}</code>\n\n"
        "Your request for an official portfolio progress report has been transmitted to our portfolio desk.\n"
        "An administrator will compile and dispatch your updated report directly via this chat.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("service:private"),
    )

    settings = get_settings(context)
    admin_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✍️ Write Report", callback_data=f"admin:write_report:{user.id}:{ref}")],
        ]
    )
    admin_text = (
        "📊 <b>NEW INVESTOR REPORT REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Investor:</b> {html.escape(user.full_name)} (@{html.escape(user.username or 'N/A')})\n"
        f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
        f"<b>Reference:</b> <code>{ref}</code>\n\n"
        "Click below to write and send a progress report to this investor."
    )
    for admin_id in settings.admin_chat_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard,
            )
        except TelegramError as exc:
            LOGGER.error("Could not notify admin %s of report request: %s", admin_id, exc)


async def admin_write_report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    settings = get_settings(context)
    admin_user = update.effective_user
    if not is_admin_user(admin_user.id if admin_user else None, context):
        await query.answer("You are not authorised.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    parts = query.data.split(":", 3)
    target_user_id = int(parts[2])
    ref = parts[3]

    context.user_data["admin_report"] = {"target_user_id": target_user_id, "reference": ref}
    await query.edit_message_text(
        query.message.text_html + f"\n\n✍️ <i>Admin {html.escape(admin_user.full_name)} is preparing report...</i>\n\n"
        "<b>Please send the report text below:</b>\n"
        "<i>(Your next text message will be forwarded directly to the investor as their portfolio report)</i>\n\n"
        "Send /cancel to abort.",
        parse_mode=ParseMode.HTML,
    )
    return ADMIN_REPORT_INPUT


async def receive_admin_report_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    report_text = (message.text or "").strip()
    data = context.user_data.get("admin_report")
    if not data:
        await message.reply_text("Session expired. Please click 'Write Report' again.")
        return ConversationHandler.END

    target_user_id = data["target_user_id"]
    ref = data["reference"]
    db = get_db(context)
    now = utc_now()

    await db.reports.update_one(
        {"reference": ref},
        {
            "$set": {
                "status": "delivered",
                "report_text": report_text,
                "sent_by": update.effective_user.id,
                "delivered_at": now,
                "updated_at": now,
            }
        },
    )

    user_msg = (
        "📊 <b>PAWNS INVESTMENT PORTFOLIO REPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Report Ref:</b> <code>{ref}</code>\n"
        f"<b>Date:</b> {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"{html.escape(report_text)}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Generated by PAWNS Portfolio Management. Contact support if you have questions.</i>"
    )
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=user_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
        await message.reply_text("✅ Report has been successfully delivered to the investor.")
    except TelegramError as exc:
        LOGGER.warning("Could not deliver report to user %s: %s", target_user_id, exc)
        await message.reply_text(f"⚠️ Could not deliver report to user: {exc}")

    context.user_data.pop("admin_report", None)
    return ConversationHandler.END


# --- Investor Withdrawal Flow ---
async def investor_withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💸 <b>Request Capital / Profit Withdrawal</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Please enter your <b>TRC20 USDT Destination Wallet Address</b> and the <b>Amount (in USD)</b> you wish to withdraw.\n\n"
        "<b>Format:</b> <code>&lt;Wallet Address&gt; &lt;Amount&gt;</code>\n"
        "<b>Example:</b> <code>TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs 500</code>\n\n"
        "⚠️ <i>Only TRC20 / TRON USDT addresses are supported for private investments.</i>\n\n"
        "Send /cancel to return to the menu.",
        parse_mode=ParseMode.HTML,
    )
    return INVESTOR_WITHDRAW_INPUT


async def receive_investor_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await message.reply_text(
            "⚠️ Please enter both your wallet address and withdrawal amount separated by a space.\n"
            "Example: <code>TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs 500</code>",
            parse_mode=ParseMode.HTML,
        )
        return INVESTOR_WITHDRAW_INPUT

    wallet = parts[0]
    amount_str = parts[1].replace("$", "").replace(",", "")

    if not validate_wallet_format(wallet, "TRC20 / TRON"):
        await message.reply_text(
            "❌ Invalid TRC20 wallet address. TRC20 addresses start with 'T' and are exactly 34 characters long.\n"
            "Please check and re-enter:"
        )
        return INVESTOR_WITHDRAW_INPUT

    try:
        amt = Decimal(amount_str)
        if amt <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        await message.reply_text("❌ Please enter a valid positive withdrawal amount.")
        return INVESTOR_WITHDRAW_INPUT

    user = update.effective_user
    db = get_db(context)
    now = utc_now()
    ref = make_reference("WTH")

    record = {
        "reference": ref,
        "telegram_id": user.id,
        "telegram_username": user.username,
        "full_name": user.full_name,
        "destination_wallet": wallet,
        "amount": str(amt),
        "currency": "USDT",
        "network": "TRC20 / TRON",
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    await db.withdrawals.insert_one(record)

    await message.reply_text(
        "⏳ <b>Withdrawal Request Logged</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{ref}</code>\n"
        f"<b>Amount:</b> ${amt} USDT\n"
        f"<b>Destination Wallet:</b> <code>{wallet}</code>\n\n"
        "Your withdrawal request has been submitted to the treasury desk for verification and disbursement.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )

    settings = get_settings(context)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Mark Paid", callback_data=f"admin:withdraw_approve:{ref}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"admin:withdraw_reject:{ref}"),
            ]
        ]
    )
    admin_text = (
        "💸 <b>NEW WITHDRAWAL REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{ref}</code>\n"
        f"<b>Investor:</b> {html.escape(user.full_name)} (@{html.escape(user.username or 'N/A')})\n"
        f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
        f"<b>Amount:</b> ${amt} USDT\n"
        f"<b>Destination:</b> <code>{wallet}</code>\n"
        f"<b>Network:</b> TRC20 / TRON"
    )
    for admin_id in settings.admin_chat_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramError as exc:
            LOGGER.error("Could not notify admin %s of withdrawal request: %s", admin_id, exc)

    return ConversationHandler.END


async def admin_withdraw_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings = get_settings(context)
    admin_user = update.effective_user
    if not is_admin_user(admin_user.id if admin_user else None, context):
        await query.answer("You are not authorised.", show_alert=True)
        return

    _, action, ref = query.data.split(":", 2)
    db = get_db(context)
    now = utc_now()
    doc = await db.withdrawals.find_one({"reference": ref})
    if not doc:
        await query.answer("Withdrawal record not found.", show_alert=True)
        return

    is_approve = action == "withdraw_approve"
    status_str = "completed" if is_approve else "rejected"

    await db.withdrawals.update_one(
        {"reference": ref},
        {"$set": {"status": status_str, "reviewed_by": admin_user.id, "updated_at": now}},
    )
    decision_label = "PROCESSED & PAID ✅" if is_approve else "REJECTED ❌"
    await query.answer(f"Withdrawal marked as {status_str}.")
    try:
        await query.edit_message_text(
            query.message.text_html + f"\n\n<b>Decision:</b> {decision_label} by {html.escape(admin_user.full_name)}",
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        pass

    target_id = doc["telegram_id"]
    if is_approve:
        user_msg = (
            "✅ <b>Withdrawal Processed</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your withdrawal of <b>${doc['amount']} USDT</b> (Ref: <code>{ref}</code>) has been successfully disbursed to your wallet:\n"
            f"<code>{doc['destination_wallet']}</code>\n\n"
            "Thank you for investing with PAWNS!"
        )
    else:
        user_msg = (
            "❌ <b>Withdrawal Request Rejected</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your withdrawal request (Ref: <code>{ref}</code>) was rejected by the treasury desk.\n\n"
            "Please contact PAWNS support for further clarification."
        )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=user_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
    except TelegramError as exc:
        LOGGER.warning("Could not notify user %s of withdrawal decision: %s", target_id, exc)


# --- Investor Termination Flow ---
async def investor_terminate_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏩ Proceed Without Reason", callback_data="term:skip_reason")],
            [InlineKeyboardButton("❌ Abort / Main Menu", callback_data="menu")],
        ]
    )
    await query.edit_message_text(
        "📄 <b>Termination of Investment Contract</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Are you sure you wish to terminate your PAWNS private investment contract?\n\n"
        "Please type a reason or feedback below, or tap <b>Proceed Without Reason</b> if you do not wish to provide one.\n\n"
        "Send /cancel to return to menu.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return INVESTOR_TERMINATE_INPUT


async def receive_termination_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await _handle_termination_request(update, context, reason="No reason provided")
    return ConversationHandler.END


async def receive_termination_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reason = clip(update.effective_message.text or "", 500)
    await _handle_termination_request(update, context, reason=reason)
    return ConversationHandler.END


async def _handle_termination_request(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str) -> None:
    user = update.effective_user
    db = get_db(context)
    now = utc_now()
    ref = make_reference("TRM")

    record = {
        "reference": ref,
        "telegram_id": user.id,
        "telegram_username": user.username,
        "full_name": user.full_name,
        "reason": reason,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    await db.terminations.insert_one(record)

    confirm_msg = (
        "⏳ <b>Termination Request Logged</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{ref}</code>\n"
        f"<b>Reason:</b> {html.escape(reason)}\n\n"
        "Your contract termination request has been sent to our administrative desk. "
        "An administrator will review and confirm the contract closure."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(confirm_msg, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
    else:
        await update.effective_message.reply_text(confirm_msg, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())

    settings = get_settings(context)
    admin_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛑 Confirm Termination", callback_data=f"admin:confirm_terminate:{user.id}:{ref}")],
        ]
    )
    admin_text = (
        "🛑 <b>INVESTMENT CONTRACT TERMINATION REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reference:</b> <code>{ref}</code>\n"
        f"<b>Investor:</b> {html.escape(user.full_name)} (@{html.escape(user.username or 'N/A')})\n"
        f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
        f"<b>Reason:</b> {html.escape(reason)}\n\n"
        "Confirming termination will set the user's status to <b>INACTIVE</b> in the database (preserving all records)."
    )
    for admin_id in settings.admin_chat_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard,
            )
        except TelegramError as exc:
            LOGGER.error("Could not notify admin %s of termination request: %s", admin_id, exc)


async def admin_confirm_terminate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings = get_settings(context)
    admin_user = update.effective_user
    if not is_admin_user(admin_user.id if admin_user else None, context):
        await query.answer("You are not authorised.", show_alert=True)
        return

    parts = query.data.split(":", 3)
    target_user_id = int(parts[2])
    ref = parts[3]
    db = get_db(context)
    now = utc_now()

    await db.users.update_one(
        {"telegram_id": target_user_id},
        {
            "$set": {
                "investor_status": "inactive",
                "termination_reference": ref,
                "terminated_at": now,
                "updated_at": now,
            }
        },
    )
    await db.terminations.update_one(
        {"reference": ref},
        {"$set": {"status": "confirmed", "reviewed_by": admin_user.id, "confirmed_at": now, "updated_at": now}},
    )

    await query.answer("Termination confirmed.")
    try:
        await query.edit_message_text(
            query.message.text_html + f"\n\n<b>Decision:</b> TERMINATED 🛑 by {html.escape(admin_user.full_name)} (Status: inactive)",
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        pass

    user_msg = (
        "ℹ️ <b>Contract Successfully Terminated</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Your PAWNS private investment contract (Ref: <code>{ref}</code>) has been officially terminated.\n\n"
        "Your account status is now set to <b>inactive</b>. All records and historical statements remain preserved.\n\n"
        "If you have questions regarding final capital settlements, please contact PAWNS Support."
    )
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=user_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
    except TelegramError as exc:
        LOGGER.warning("Could not notify user %s of contract termination: %s", target_user_id, exc)


async def admin_audit_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
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
    if not await require_admin(update, context):
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
    if not await require_admin(update, context):
        return
    settings = get_settings(context)
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
            "program": "trading_subscriptions",
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


async def admin_add_investment_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /addinvestmentprofit &lt;investor_telegram_id&gt; &lt;profit_amount&gt; [note]\n"
            "Example: <code>/addinvestmentprofit 123456789 250 Month 1 trading profit</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        investor_id = int(context.args[0])
        profit_amount = Decimal(context.args[1])
    except (ValueError, InvalidOperation):
        await update.effective_message.reply_text("Investor ID must be an integer and profit amount must be numeric.")
        return

    if profit_amount <= 0:
        await update.effective_message.reply_text("Profit amount must be greater than zero.")
        return

    db = get_db(context)
    investor = await db.users.find_one({"telegram_id": investor_id})
    if not investor:
        await update.effective_message.reply_text("Investor account not found.")
        return

    referrer_id = investor.get("referred_by")
    if not referrer_id:
        await update.effective_message.reply_text("This investor was not referred by anyone. No referral commission recorded.")
        return

    referrer = await db.users.find_one({"telegram_id": referrer_id})
    if not referrer:
        await update.effective_message.reply_text("Referrer account not found.")
        return

    share = (profit_amount * INVESTMENT_REFERRAL_RATE / Decimal("100")).quantize(Decimal("0.01"))
    event_reference = make_reference("COM")
    now = utc_now()
    note = clip(" ".join(context.args[2:]), 300) if len(context.args) > 2 else ""

    com_doc = {
        "reference": event_reference,
        "referrer_telegram_id": referrer_id,
        "referred_telegram_id": investor_id,
        "program": "private_investment",
        "service": "private",
        "service_name": "PAWNS Private Investment",
        "profit_amount": str(profit_amount),
        "commission_rate": str(INVESTMENT_REFERRAL_RATE),
        "referrer_share": str(share),
        "currency": "USDT",
        "note": note,
        "status": "verified",
        "created_by": update.effective_user.id,
        "created_at": now,
    }
    await db.commissions.insert_one(com_doc)

    try:
        await context.bot.send_message(
            chat_id=referrer_id,
            text=(
                "🎉 <b>Private Investment Profit Commission Credited!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Your referred investor earned profits on their PAWNS Private Investment portfolio.\n\n"
                f"• <b>Realized Profit:</b> ${profit_amount} USDT\n"
                f"• <b>Referral Rate:</b> <b>{INVESTMENT_REFERRAL_RATE}%</b>\n"
                f"• <b>Your Commission:</b> <b>+{share} USDT</b>\n"
                f"• <b>Ref:</b> <code>{event_reference}</code>\n\n"
                "Check your balance with /referral."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        LOGGER.warning("Could not send investment profit commission alert to %s: %s", referrer_id, exc)

    await update.effective_message.reply_text(
        f"Credited 10% referral commission (+{share} USDT) to referrer {referrer_id} for investor {investor_id}.\nReference: {event_reference}"
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

    # Sync database admins
    settings: Settings = application.bot_data["settings"]
    admins = set(settings.admin_chat_ids)
    try:
        cursor = db.users.find({"is_admin": True})
        async for doc in cursor:
            if "telegram_id" in doc:
                try:
                    admins.add(int(doc["telegram_id"]))
                except (ValueError, TypeError):
                    pass
        application.bot_data["admin_ids"] = admins
        LOGGER.info("Admin system initialized with %d authorized administrators", len(admins))
    except Exception as exc:
        LOGGER.warning("Could not sync db admins in post_init: %s", exc)

    try:
        # Default scope: all regular users in private chats see ONLY standard user commands
        await application.bot.set_my_commands(
            USER_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        # Dedicated scope: each authorized administrator sees the admin management suite
        for admin_id in admins:
            try:
                await application.bot.set_my_commands(
                    ADMIN_COMMANDS,
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception as exc:
                LOGGER.warning("Could not set admin commands for %s: %s", admin_id, exc)
    except Exception as exc:
        LOGGER.warning("Could not register scoped bot commands: %s", exc)
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
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["admin_ids"] = set(settings.admin_chat_ids)

    registration = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(registration_start, pattern=r"^register:(private|crypto|forex_live|forex_prop|synthetic)$"),
            CallbackQueryHandler(crypto_pay_start, pattern=r"^cf_pay:(bingx|standard):(1m|3m|6m|12m)$"),
            CallbackQueryHandler(forex_pay_start, pattern=r"^forex_pay:(live|prop):(1m|3m|6m|12m)$"),
            CallbackQueryHandler(onboard_start, pattern=r"^onboard_start:BM-\d{8}-[A-F0-9]{8}$"),
        ],
        states={
            FULL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_full_name)],
            INVESTMENT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_investment_amount)],
            RISK_CATEGORY: [
                CallbackQueryHandler(select_risk, pattern=r"^risk:(high|low)$"),
                CallbackQueryHandler(show_risk_info, pattern=r"^risk:info$"),
                CallbackQueryHandler(back_to_risk, pattern=r"^risk:back$"),
            ],
            DURATION: [CallbackQueryHandler(select_duration, pattern=r"^duration:(2m|3m|6m|12m)$")],
            CONSENT: [CallbackQueryHandler(receive_consent, pattern=r"^consent:(yes|no)$")],
            SELECT_PAYMENT_METHOD: [CallbackQueryHandler(receive_payment_method, pattern=r"^paymethod:(crypto|naira)$")],
            PAYMENT_DETAILS: [CallbackQueryHandler(receive_payment_button, pattern=r"^pay:(confirm|confirm_naira|cancel)$")],
            AWAIT_TXID: [
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    receive_txid,
                )
            ],
            AWAIT_NAIRA_RECEIPT: [
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    receive_naira_receipt,
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

    bingx_uid_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bingx_uid_start, pattern=r"^bingx:enter_uid$"),
        ],
        states={
            BINGX_UID_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_bingx_uid)],
        },
        fallbacks=[
            CommandHandler(["cancel", "menu", "start", "stop"], cancel_registration),
        ],
        allow_reentry=True,
        name="bingx_uid",
    )
    application.add_handler(bingx_uid_conv)

    investor_withdraw_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(investor_withdraw_start, pattern=r"^inv:withdraw$"),
        ],
        states={
            INVESTOR_WITHDRAW_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_investor_withdraw)],
        },
        fallbacks=[
            CommandHandler(["cancel", "menu", "start", "stop"], cancel_registration),
        ],
        allow_reentry=True,
        name="investor_withdraw",
    )
    application.add_handler(investor_withdraw_conv)

    investor_terminate_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(investor_terminate_start, pattern=r"^inv:terminate$"),
        ],
        states={
            INVESTOR_TERMINATE_INPUT: [
                CallbackQueryHandler(receive_termination_skip, pattern=r"^term:skip_reason$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_termination_reason),
            ],
        },
        fallbacks=[
            CommandHandler(["cancel", "menu", "start", "stop"], cancel_registration),
        ],
        allow_reentry=True,
        name="investor_terminate",
    )
    application.add_handler(investor_terminate_conv)

    admin_report_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_write_report_start, pattern=r"^admin:write_report:\d+:[A-Z0-9-]+$"),
        ],
        states={
            ADMIN_REPORT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_report_text)],
        },
        fallbacks=[
            CommandHandler(["cancel", "menu", "start", "stop"], cancel_registration),
        ],
        allow_reentry=True,
        name="admin_report",
    )
    application.add_handler(admin_report_conv)

    admin_announcement_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["announcement", "broadcast"], admin_announcement_start),
            CallbackQueryHandler(admin_announcement_start, pattern=r"^admin:announce$"),
        ],
        states={
            ANNOUNCEMENT_TEXT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_announcement_text),
                CallbackQueryHandler(admin_announcement_cancel, pattern=r"^announce:cancel$"),
            ],
            ANNOUNCEMENT_CONFIRM: [
                CallbackQueryHandler(admin_announcement_proceed, pattern=r"^announce:proceed$"),
                CallbackQueryHandler(admin_announcement_cancel, pattern=r"^announce:cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler(["cancel", "menu", "start", "stop"], admin_announcement_cancel),
        ],
        allow_reentry=True,
        name="admin_announcement",
    )
    application.add_handler(admin_announcement_conv)

    admin_verify_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_review_verify_entry, pattern=r"^admin:verify:BM-\d{8}-[A-F0-9]{8}$"),
        ],
        states={
            ADMIN_VERIFY_LINK_INPUT: [
                CallbackQueryHandler(receive_admin_verify_default, pattern=r"^admin:verify_default:BM-\d{8}-[A-F0-9]{8}$"),
                CallbackQueryHandler(receive_admin_verify_cancel, pattern=r"^admin:verify_cancel:BM-\d{8}-[A-F0-9]{8}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_verify_link),
            ],
        },
        fallbacks=[
            CommandHandler(["cancel", "menu", "start", "stop"], admin_verify_cancel_cmd),
            CallbackQueryHandler(receive_admin_verify_cancel, pattern=r"^admin:verify_cancel:BM-\d{8}-[A-F0-9]{8}$"),
        ],
        allow_reentry=True,
        name="admin_verify",
    )
    application.add_handler(admin_verify_conv)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["menu", "services", "cancel", "stop"], show_main_menu))
    application.add_handler(CommandHandler("investment", show_private))
    application.add_handler(CommandHandler("crypto", show_crypto))
    application.add_handler(CommandHandler("forex", show_forex))
    application.add_handler(CommandHandler("synthetic", show_synthetic))
    application.add_handler(CommandHandler("referral", show_referral))
    application.add_handler(CommandHandler("support", show_support))
    application.add_handler(CommandHandler("terms", show_terms))
    application.add_handler(CommandHandler(["id", "myid", "whoami"], cmd_my_id))

    # Admin commands
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CommandHandler(["admin", "panel"], admin_panel_menu))
    application.add_handler(CommandHandler("settings", admin_settings))
    application.add_handler(CommandHandler("admins", admin_list_admins))
    application.add_handler(CommandHandler("addadmin", admin_add_admin))
    application.add_handler(CommandHandler("removeadmin", admin_remove_admin))
    application.add_handler(CommandHandler("setwallet", admin_set_wallet))
    application.add_handler(CommandHandler("setfee", admin_set_fee))
    application.add_handler(CommandHandler("setmininvest", admin_set_min_invest))
    application.add_handler(CommandHandler("setnetwork", admin_set_network))
    application.add_handler(CommandHandler("setinstructions", admin_set_instructions))
    application.add_handler(CommandHandler("setnairarate", admin_set_naira_rate))
    application.add_handler(CommandHandler("checkexpiry", admin_check_expiry))
    application.add_handler(CommandHandler(["audit", "auditlog"], admin_audit_log))
    application.add_handler(CommandHandler("setlink", admin_set_link))
    application.add_handler(CommandHandler("addcommission", admin_add_commission))
    application.add_handler(CommandHandler("addinvestmentprofit", admin_add_investment_profit))

    application.add_handler(
        CallbackQueryHandler(
            admin_review,
            pattern=r"^admin:(verify|reject|reqinfo):BM-\d{8}-[A-F0-9]{8}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin_bingx_review,
            pattern=r"^admin:bingx_(approve|reject):\d+:.+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin_withdraw_review,
            pattern=r"^admin:(withdraw_approve|withdraw_reject):WTH-\d{8}-[A-F0-9]{8}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin_confirm_terminate,
            pattern=r"^admin:confirm_terminate:\d+:TRM-\d{8}-[A-F0-9]{8}$",
        )
    )
    application.add_handler(CallbackQueryHandler(not_configured, pattern=r"^not_configured:"))
    application.add_handler(
        CallbackQueryHandler(
            route_menu_callback,
            pattern=r"^(menu|about|support|terms(:investment)?|referral(:tiers)?|service:.+|crypto:.+|forex_(live|prop):durations|inv:report|admin:(menu|stats|pending|settings_view|manage|audit))$",
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    application.add_error_handler(error_handler)

    if application.job_queue:
        FOUR_DAYS_SECONDS = 4 * 24 * 3600
        application.job_queue.run_repeating(
            subscription_expiry_job,
            interval=FOUR_DAYS_SECONDS,
            first=60,
            name="subscription_expiry_check",
        )

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
