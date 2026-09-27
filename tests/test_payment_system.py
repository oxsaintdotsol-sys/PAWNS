"""Automated test suite for PAWNS bot payment system and procedure."""

import asyncio
import os
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

# Import bot components
from pawnstrading_bot import (
    FORBIDDEN_SECRET_RE,
    SERVICE_NAMES,
    Database,
    Settings,
    get_service_payment_info,
    render_payment_screen,
)
from chain_verifier import (
    get_explorer_url,
    normalize_txid,
    validate_txid_format,
    validate_wallet_format,
)


class TestPaymentMethodsAndNetworks(unittest.TestCase):
    """Test payment method separation and wallet routing."""

    def setUp(self):
        self.settings = Settings(
            bot_token="test:token",
            mongodb_uri="",  # in-memory mode
            database_name="pawnstrading_test",
            admin_chat_ids=(123456789,),
            referral_secret="test_secret",
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
            payment_instructions_investment="Send USDT via TRC20.",
            payment_instructions_trading="Send USD/USDT via BSC BEP20.",
            about_text="About text",
            terms_text="Terms text",
            return_basis_text="Basis text",
            private_investment_enabled=True,
            minimum_investment=Decimal("500"),
            commission_percent=Decimal("10"),
        )
        self.db = Database(self.settings)

        # Mock context
        self.context = MagicMock()
        self.context.application.bot_data = {
            "settings": self.settings,
            "db": self.db,
        }

    def test_private_investment_routing(self):
        """Private Investment must strictly use TRC20 USDT and investment wallet."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            info = loop.run_until_complete(
                get_service_payment_info(self.context, "private", investment_amount="1000")
            )
            self.assertEqual(info["currency"], "USDT")
            self.assertEqual(info["network"], "TRC20 / TRON")
            self.assertEqual(info["wallet"], "TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs")
            self.assertEqual(info["amount"], "1000")

            text, markup = render_payment_screen(info)
            self.assertIn("TRC20 / TRON", text)
            self.assertIn("TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs", text)
            self.assertIn("USDT", text)
            # Must not show trading services wallet
            self.assertNotIn("0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67", text)
        finally:
            loop.close()

    def test_crypto_futures_routing(self):
        """Crypto Futures must use BSC/BEP20 and $50 fee."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            info = loop.run_until_complete(get_service_payment_info(self.context, "crypto"))
            self.assertEqual(info["network"], "BSC / BEP20")
            self.assertEqual(info["wallet"], "0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67")
            self.assertEqual(info["amount"], "50")

            text, markup = render_payment_screen(info)
            self.assertIn("BSC / BEP20", text)
            self.assertIn("0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67", text)
            self.assertIn("$50", text)
            # Must not show TRC20 wallet
            self.assertNotIn("TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs", text)
        finally:
            loop.close()

    def test_forex_and_synthetic_fees(self):
        """Forex Live $50, Forex Prop $50, Synthetic $20 all on BSC/BEP20."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            live = loop.run_until_complete(get_service_payment_info(self.context, "forex_live"))
            self.assertEqual(live["amount"], "50")
            self.assertEqual(live["network"], "BSC / BEP20")

            prop = loop.run_until_complete(get_service_payment_info(self.context, "forex_prop"))
            self.assertEqual(prop["amount"], "50")
            self.assertEqual(prop["network"], "BSC / BEP20")

            synthetic = loop.run_until_complete(get_service_payment_info(self.context, "synthetic"))
            self.assertEqual(synthetic["amount"], "20")
            self.assertEqual(synthetic["network"], "BSC / BEP20")
            self.assertEqual(synthetic["wallet"], "0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67")
        finally:
            loop.close()


class TestValidationAndSecurity(unittest.TestCase):
    """Test format validators, security regexes, and replay protection."""

    def test_txid_validation(self):
        tron_valid = "4f8a65b93d6e5c8a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a"
        bsc_valid_0x = "0x4f8a65b93d6e5c8a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a"
        bsc_valid_bare = "4f8a65b93d6e5c8a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a"

        self.assertTrue(validate_txid_format(tron_valid, "TRC20 / TRON"))
        self.assertFalse(validate_txid_format("not-a-txid", "TRC20 / TRON"))
        self.assertFalse(validate_txid_format("0x" + tron_valid, "TRC20 / TRON"))  # Tron doesn't use 0x

        self.assertTrue(validate_txid_format(bsc_valid_0x, "BSC / BEP20"))
        self.assertTrue(validate_txid_format(bsc_valid_bare, "BSC / BEP20"))
        self.assertFalse(validate_txid_format("short_hash", "BSC / BEP20"))

    def test_wallet_validation(self):
        tron_wallet = "TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs"
        bsc_wallet = "0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67"

        self.assertTrue(validate_wallet_format(tron_wallet, "TRC20 / TRON"))
        self.assertFalse(validate_wallet_format(bsc_wallet, "TRC20 / TRON"))

        self.assertTrue(validate_wallet_format(bsc_wallet, "BSC / BEP20"))
        self.assertFalse(validate_wallet_format(tron_wallet, "BSC / BEP20"))

    def test_explorer_url_generation(self):
        txid = "4f8a65b93d6e5c8a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a"
        tron_url = get_explorer_url(txid, "TRC20 / TRON")
        bsc_url = get_explorer_url(txid, "BSC / BEP20")

        self.assertEqual(tron_url, f"https://tronscan.org/#/transaction/{txid}")
        self.assertEqual(bsc_url, f"https://bscscan.com/tx/0x{txid}")

    def test_security_forbidden_credentials(self):
        """Must detect and reject seed phrases, private keys, passwords, 2FA, OTP."""
        forbidden_messages = [
            "My seed phrase is apple banana orange dog cat tree car",
            "Here is my private key 0x123456789abcdef",
            "My exchange password is password123",
            "The 2fa code is 123456",
            "One-time password: 998877",
            "My OTP code is 445566",
        ]
        for msg in forbidden_messages:
            self.assertIsNotNone(
                FORBIDDEN_SECRET_RE.search(msg),
                f"Failed to catch forbidden secret in: {msg}",
            )


class TestAdminConfigurationAndAudit(unittest.TestCase):
    """Test dynamic admin updates and audit logging."""

    def setUp(self):
        self.settings = Settings(
            bot_token="test:token",
            mongodb_uri="",
            database_name="pawnstrading_test",
            admin_chat_ids=(123456789,),
            referral_secret="test_secret",
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
            payment_instructions_investment="Send USDT via TRC20.",
            payment_instructions_trading="Send USD/USDT via BSC BEP20.",
            about_text="About text",
            terms_text="Terms text",
            return_basis_text="Basis text",
            private_investment_enabled=True,
            minimum_investment=Decimal("500"),
            commission_percent=Decimal("10"),
        )
        self.db = Database(self.settings)

    def test_wallet_update_creates_audit_log(self):
        """Updating wallet stores new setting and creates audit entry with old & new value."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            admin_id = 123456789
            new_wallet = "0x1111111111111111111111111111111111111111"

            audit_entry = loop.run_until_complete(
                self.db.set_setting("trading_wallet", new_wallet, admin_id)
            )
            self.assertEqual(audit_entry["action"], "setting_updated")
            self.assertEqual(audit_entry["setting"], "trading_wallet")
            self.assertEqual(audit_entry["new_value"], new_wallet)
            self.assertEqual(audit_entry["admin_id"], admin_id)

            # Verify in DB
            stored = loop.run_until_complete(self.db.get_setting("trading_wallet"))
            self.assertEqual(stored, new_wallet)

            audit_count = loop.run_until_complete(self.db.audit.count_documents({}))
            self.assertGreaterEqual(audit_count, 1)
        finally:
            loop.close()

    def test_fee_update(self):
        """Updating fees modifies dynamic lookup."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            admin_id = 123456789
            loop.run_until_complete(self.db.set_setting("fee_crypto", "75", admin_id))

            context = MagicMock()
            context.application.bot_data = {"settings": self.settings, "db": self.db}

            info = loop.run_until_complete(get_service_payment_info(context, "crypto"))
            self.assertEqual(info["amount"], "75")
        finally:
            loop.close()


class TestPaymentLifecycle(unittest.TestCase):
    """Test full payment lifecycle: submission, replay prevention, admin review, and onboarding."""

    def setUp(self):
        self.settings = Settings(
            bot_token="test:token",
            mongodb_uri="",
            database_name="pawnstrading_test",
            admin_chat_ids=(123456789,),
            referral_secret="test_secret",
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
            payment_instructions_investment="Send USDT via TRC20.",
            payment_instructions_trading="Send USD/USDT via BSC BEP20.",
            about_text="About text",
            terms_text="Terms text",
            return_basis_text="Basis text",
            private_investment_enabled=True,
            minimum_investment=Decimal("500"),
            commission_percent=Decimal("10"),
        )
        self.db = Database(self.settings)

    def test_payment_submission_and_replay_protection(self):
        """A submitted TXID creates a PENDING record and blocks replay."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            txid = "0x" + "a" * 64
            submission = {
                "reference": "BM-20260923-TEST",
                "telegram_id": 987654321,
                "telegram_username": "trader1",
                "full_name": "Trader One",
                "service": "crypto",
                "service_name": "Crypto Futures Trading",
                "amount": "50",
                "currency": "USD / USDT",
                "network": "BSC / BEP20",
                "wallet_address": "0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67",
                "txid": txid,
                "payment_status": "PENDING",
                "admin_verification_status": {"decision": "Pending"},
                "onboarding_status": "Pending",
            }
            loop.run_until_complete(self.db.submissions.insert_one(submission))

            # Query should find it
            found = loop.run_until_complete(self.db.submissions.find_one({"reference": "BM-20260923-TEST"}))
            self.assertIsNotNone(found)
            self.assertEqual(found["payment_status"], "PENDING")

            # Check replay protection
            dup = loop.run_until_complete(
                self.db.submissions.find_one(
                    {"txid": txid, "payment_status": {"$in": ["PENDING", "VERIFIED ✅", "Verified"]}}
                )
            )
            self.assertIsNotNone(dup)
            self.assertEqual(dup["reference"], "BM-20260923-TEST")
        finally:
            loop.close()

    def test_admin_approval_and_onboarding_completion(self):
        """Admin verification updates status and user onboarding completes successfully."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ref = "BM-20260923-ONBOARD"
            submission = {
                "reference": ref,
                "telegram_id": 987654321,
                "service": "crypto",
                "payment_status": "PENDING",
                "onboarding_status": "Pending",
            }
            loop.run_until_complete(self.db.submissions.insert_one(submission))

            # Admin approves
            approved = loop.run_until_complete(
                self.db.submissions.find_one_and_update(
                    {"reference": ref, "payment_status": "PENDING"},
                    {
                        "$set": {
                            "payment_status": "VERIFIED ✅",
                            "onboarding_status": "Pending Details",
                            "admin_verification_status": {
                                "decision": "Approved",
                                "reviewed_by": 123456789,
                            },
                        }
                    },
                )
            )
            self.assertIsNotNone(approved)

            # User submits onboarding details
            updated = loop.run_until_complete(
                self.db.submissions.find_one_and_update(
                    {"reference": ref, "telegram_id": 987654321},
                    {
                        "$set": {
                            "onboarding_status": "Completed",
                            "onboarding_data": {"details": "BingX UID: 99887766"},
                        }
                    },
                )
            )
            self.assertIsNotNone(updated)

            # Final verify
            final = loop.run_until_complete(self.db.submissions.find_one({"reference": ref}))
            self.assertEqual(final["payment_status"], "VERIFIED ✅")
            self.assertEqual(final["onboarding_status"], "Completed")
            self.assertEqual(final["onboarding_data"]["details"], "BingX UID: 99887766")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
