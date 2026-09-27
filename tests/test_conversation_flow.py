"""Unit tests for conversation flow, cancel behavior, and single-handler execution."""

import asyncio
import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Update
from pawnstrading_bot import build_application, Settings


def make_test_settings() -> Settings:
    return Settings(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        mongodb_uri="",
        database_name="test_db",
        admin_chat_ids=(123456789,),
        referral_secret="secret123",
        run_mode="polling",
        webhook_base_url="",
        webhook_path="telegram",
        webhook_secret="",
        port=8080,
        investment_wallet="TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs",
        trading_wallet="0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67",
        investment_network="TRC20 / TRON",
        trading_network="BSC / BEP20",
        fee_crypto=Decimal("50"),
        fee_forex_live=Decimal("50"),
        fee_forex_prop=Decimal("50"),
        fee_synthetic=Decimal("20"),
        payment_instructions_investment="",
        payment_instructions_trading="",
        about_text="about",
        terms_text="terms",
        return_basis_text="return",
        private_investment_enabled=True,
        minimum_investment=Decimal("500"),
        commission_percent=Decimal("10"),
    )


class TestConversationAndCancelFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = make_test_settings()
        self.app = build_application(self.settings)

        # Mock Telegram server API responses
        def mock_request(url, *args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            
            data = kwargs.get("json", {}) or kwargs.get("data", {})
            if "getMe" in str(url):
                res = {"id": 999, "first_name": "Bot", "is_bot": True, "username": "test_bot"}
            else:
                res = {
                    "message_id": 999,
                    "date": 1600000000,
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 999, "is_bot": True, "first_name": "Bot"},
                    "text": str(data.get("text", "")),
                }
            mock_resp.content = json.dumps({"ok": True, "result": res}).encode()
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.json.return_value = {"ok": True, "result": res}
            return mock_resp

        self.patcher = patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=mock_request))
        self.patcher.start()

        await self.app.initialize()

    async def asyncTearDown(self) -> None:
        await self.app.shutdown()
        self.patcher.stop()

    def make_update(self, text: str, user_id: int = 12345, update_id: int = 1) -> Update:
        data = {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1600000000,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "TestUser"},
                "text": text,
            },
        }
        if text.startswith("/"):
            data["message"]["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text)}
            ]
        return Update.de_json(data, self.app.bot)

    def make_callback_update(self, data_str: str, user_id: int = 12345, update_id: int = 1) -> Update:
        data = {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "from": {"id": user_id, "is_bot": False, "first_name": "TestUser"},
                "message": {
                    "message_id": update_id,
                    "date": 1600000000,
                    "chat": {"id": user_id, "type": "private"},
                    "text": "menu",
                },
                "chat_instance": "123",
                "data": data_str,
            },
        }
        return Update.de_json(data, self.app.bot)

    async def test_input_during_registration_does_not_trigger_unknown_message(self) -> None:
        """When user provides name or amount, the bot must only send the next question, not 'unknown_message'."""
        replies: list[str] = []

        async def capture_reply(text, **kwargs):
            replies.append(text)
            mock_msg = MagicMock()
            mock_msg.message_id = 99
            return mock_msg

        with patch("telegram.Message.reply_text", side_effect=capture_reply):
            # 1. User starts private investment registration
            cb_update = self.make_callback_update("register:private", update_id=1)
            await self.app.process_update(cb_update)

            # 2. User submits full name
            replies.clear()
            msg_update = self.make_update("Mubarak Mohammed", update_id=2)
            await self.app.process_update(msg_update)

            # Must have asked for investment amount
            self.assertTrue(any("how much would you like to invest" in r.lower() for r in replies))
            # Must NOT have sent 'unknown_message'
            self.assertFalse(any("did not understand that message" in r.lower() for r in replies))
            self.assertEqual(len(replies), 1)

    async def test_cancel_during_registration_sends_single_clean_menu(self) -> None:
        """When user cancels with /cancel, it should send the main menu once with no 'Registration cancelled' banner."""
        replies: list[str] = []

        async def capture_reply(text, **kwargs):
            replies.append(text)
            mock_msg = MagicMock()
            mock_msg.message_id = 99
            return mock_msg

        with patch("telegram.Message.reply_text", side_effect=capture_reply):
            # 1. User starts registration
            cb_update = self.make_callback_update("register:private", update_id=1)
            await self.app.process_update(cb_update)

            # 2. User sends /cancel
            replies.clear()
            cancel_update = self.make_update("/cancel", update_id=2)
            await self.app.process_update(cancel_update)

            # Should have sent exactly ONE reply
            self.assertEqual(len(replies), 1)
            # Should be the clean PAWNS main menu
            self.assertIn("PAWNS BOT", replies[0])
            # Must NOT contain "Registration cancelled" banner
            self.assertNotIn("Registration cancelled", replies[0])

    async def test_unknown_message_outside_conversation(self) -> None:
        """When user sends unrecognized text outside of conversation, unknown_message handler triggers."""
        replies: list[str] = []

        async def capture_reply(text, **kwargs):
            replies.append(text)
            mock_msg = MagicMock()
            mock_msg.message_id = 99
            return mock_msg

        with patch("telegram.Message.reply_text", side_effect=capture_reply):
            msg_update = self.make_update("random question", update_id=10)
            await self.app.process_update(msg_update)

            self.assertEqual(len(replies), 1)
            self.assertIn("I did not understand that message", replies[0])


if __name__ == "__main__":
    unittest.main()
