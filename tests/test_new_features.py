"""Automated test suite for new PAWNS features:
- Dual track BingX vs Standard pricing
- Naira bank payment calculations and admin exchange rate commands
- BingX UID verification flow
- Investor Hub (reports, withdrawals, contract termination without user deletion)
- Subscription expiry background job and notifications (4-day cycle)
- Partner brokers and prop firms configuration
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, MagicMock

from pawnstrading_bot import (
    CRYPTO_BINGX_FEES,
    CRYPTO_STANDARD_FEES,
    FOREX_FEES,
    Database,
    Settings,
    admin_confirm_terminate,
    admin_set_naira_rate,
    admin_bingx_review,
    get_service_payment_info,
    receive_bingx_uid,
    receive_investor_withdraw,
    receive_payment_method,
    receive_termination_skip,
    render_naira_payment_screen,
    run_subscription_expiry_check,
    utc_now,
    prompt_payment_method,
    SELECT_PAYMENT_METHOD,
    PAYMENT_DETAILS,
    TRADING_REFERRAL_TIERS,
    INVESTMENT_REFERRAL_RATE,
    get_trading_referral_tier,
    process_referral_on_payment_verified,
    admin_add_investment_profit,
    show_referral,
    show_referral_tiers,
)


class TestNewFeatures(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            bot_token="test:token",
            mongodb_uri="",  # in-memory mode
            database_name="pawnstrading_test",
            admin_chat_ids=(123456789, 1083331304),
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
            usd_ngn_rate=Decimal("1400"),
            naira_bank_name="Zenith Bank",
            naira_account_number="1234567890",
            naira_account_name="PAWNS Trading Services",
            broker_1_name="Exness",
            broker_2_name="HFM (HotForex)",
            broker_3_name="Deriv Forex",
            prop_1_name="Naira Trader",
            prop_2_name="Naira Prop",
            prop_3_name="Global Dollar Prop",
        )
        self.db = Database(self.settings)

        self.context = MagicMock()
        self.context.application.bot_data = {
            "settings": self.settings,
            "db": self.db,
        }
        self.context.bot.send_message = AsyncMock()
        self.context.application.bot.send_message = AsyncMock()

    def test_bingx_and_standard_pricing_tiers(self):
        """Verify BingX discounted pricing tiers and Standard pricing tiers."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # BingX tiers: 1m=$40, 3m=$90, 6m=$200, 12m=$300
            self.assertEqual(CRYPTO_BINGX_FEES["1m"], Decimal("40"))
            self.assertEqual(CRYPTO_BINGX_FEES["3m"], Decimal("90"))
            self.assertEqual(CRYPTO_BINGX_FEES["6m"], Decimal("200"))
            self.assertEqual(CRYPTO_BINGX_FEES["12m"], Decimal("300"))

            # Standard tiers: 1m=$100, 3m=$149.9, 6m=$400, 12m=$500
            self.assertEqual(CRYPTO_STANDARD_FEES["1m"], Decimal("100"))
            self.assertEqual(CRYPTO_STANDARD_FEES["3m"], Decimal("149.9"))
            self.assertEqual(CRYPTO_STANDARD_FEES["6m"], Decimal("400"))
            self.assertEqual(CRYPTO_STANDARD_FEES["12m"], Decimal("500"))

            # Forex tiers: 1m=$100, 3m=$200, 6m=$400, 12m=$500
            self.assertEqual(FOREX_FEES["1m"], Decimal("100"))
            self.assertEqual(FOREX_FEES["12m"], Decimal("500"))

            # Verify get_service_payment_info returns correct tier
            bingx_1m = loop.run_until_complete(
                get_service_payment_info(self.context, "crypto", duration="1m", track="bingx")
            )
            self.assertEqual(bingx_1m["amount"], "40")

            bingx_1yr = loop.run_until_complete(
                get_service_payment_info(self.context, "crypto", duration="12m", track="bingx")
            )
            self.assertEqual(bingx_1yr["amount"], "300")

            std_1m = loop.run_until_complete(
                get_service_payment_info(self.context, "crypto", duration="1m", track="standard")
            )
            self.assertEqual(std_1m["amount"], "100")
        finally:
            loop.close()

    def test_naira_payment_calculation_and_admin_rate(self):
        """Verify Naira payment screen calculation and dynamic rate update via admin command."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            payment_info = {
                "service": "crypto",
                "service_name": "Crypto Futures",
                "amount": "40",
                "currency": "USD",
                "network": "BSC / BEP20",
                "wallet": "0x123",
                "instructions": "Pay BSC",
            }
            # At 1400 NGN/USD, $40 is 56,000 NGN
            text, _ = render_naira_payment_screen(payment_info, self.settings, Decimal("1400"))
            self.assertIn("₦56,000 NGN", text)
            self.assertIn("Zenith Bank", text)
            self.assertIn("1234567890", text)

            # Update rate using /setnairarate command
            update = MagicMock()
            update.effective_user.id = 123456789  # admin
            update.effective_message.reply_text = AsyncMock()
            self.context.args = ["1450"]

            loop.run_until_complete(admin_set_naira_rate(update, self.context))
            update.effective_message.reply_text.assert_called_once()
            self.assertIn("₦1450", update.effective_message.reply_text.call_args[0][0])

            # Verify rate was updated in db
            saved_rate = loop.run_until_complete(self.db.get_setting("usd_ngn_rate"))
            self.assertEqual(saved_rate, "1450")

            # At new rate 1450, $40 is 58,000 NGN
            text2, _ = render_naira_payment_screen(payment_info, self.settings, Decimal(saved_rate))
            self.assertIn("₦58,000 NGN", text2)
        finally:
            loop.close()

    def test_bingx_uid_submission_and_admin_approval(self):
        """Test user submitting BingX UID and admin approving it to unlock discounted rates."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Create user doc
            user_id = 998877
            loop.run_until_complete(
                self.db.users.insert_one({
                    "telegram_id": user_id,
                    "full_name": "Test Trader",
                    "bingx_verified": False,
                })
            )

            # User sends UID
            update = MagicMock()
            update.effective_user.id = user_id
            update.effective_user.full_name = "Test Trader"
            update.effective_user.username = "testtrader"
            update.effective_message.text = "987654321"
            update.effective_message.reply_text = AsyncMock()

            loop.run_until_complete(receive_bingx_uid(update, self.context))
            update.effective_message.reply_text.assert_called_once()

            # Verify record created in db.bingx_verifications
            verification = loop.run_until_complete(self.db.bingx_verifications.find_one({"telegram_id": user_id}))
            self.assertIsNotNone(verification)
            self.assertEqual(verification["uid"], "987654321")
            self.assertEqual(verification["status"], "pending")

            # Admin approves UID
            admin_update = MagicMock()
            admin_update.callback_query.data = f"admin:bingx_approve:{user_id}:987654321"
            admin_update.callback_query.answer = AsyncMock()
            admin_update.callback_query.edit_message_text = AsyncMock()
            admin_update.effective_user.id = 123456789  # admin
            admin_update.effective_user.full_name = "Admin Alice"

            loop.run_until_complete(admin_bingx_review(admin_update, self.context))

            # User document in DB should now have bingx_verified=True
            user_doc = loop.run_until_complete(self.db.users.find_one({"telegram_id": user_id}))
            self.assertTrue(user_doc.get("bingx_verified"))
            self.assertEqual(user_doc.get("bingx_uid"), "987654321")

            # Verification record should be approved
            ver_updated = loop.run_until_complete(self.db.bingx_verifications.find_one({"telegram_id": user_id}))
            self.assertEqual(ver_updated["status"], "approved")
        finally:
            loop.close()

    def test_investor_withdrawal_and_contract_termination_preserves_user(self):
        """Test investor withdrawal request and contract termination (user set to inactive, NOT deleted)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            user_id = 554433
            loop.run_until_complete(
                self.db.users.insert_one({
                    "telegram_id": user_id,
                    "full_name": "Active Investor",
                    "investor_status": "active",
                })
            )

            # Test Withdrawal
            w_update = MagicMock()
            w_update.effective_user.id = user_id
            w_update.effective_user.full_name = "Active Investor"
            w_update.effective_user.username = "actinv"
            w_update.effective_message.text = "TGJTYkkXpPg8Mi2jYLWFxx4YWSoVTY3tUs 1500"
            w_update.effective_message.reply_text = AsyncMock()

            loop.run_until_complete(receive_investor_withdraw(w_update, self.context))
            wth_record = loop.run_until_complete(self.db.withdrawals.find_one({"telegram_id": user_id}))
            self.assertIsNotNone(wth_record)
            self.assertEqual(wth_record["amount"], "1500")
            self.assertEqual(wth_record["network"], "TRC20 / TRON")

            # Test Contract Termination (Skip reason)
            t_update = MagicMock()
            t_update.effective_user.id = user_id
            t_update.effective_user.full_name = "Active Investor"
            t_update.effective_user.username = "actinv"
            t_update.callback_query.data = "term:skip_reason"
            t_update.callback_query.answer = AsyncMock()
            t_update.callback_query.edit_message_text = AsyncMock()

            loop.run_until_complete(receive_termination_skip(t_update, self.context))
            term_record = loop.run_until_complete(self.db.terminations.find_one({"telegram_id": user_id}))
            self.assertIsNotNone(term_record)
            self.assertEqual(term_record["reason"], "No reason provided")
            term_ref = term_record["reference"]

            # Admin confirms termination
            admin_update = MagicMock()
            admin_update.callback_query.data = f"admin:confirm_terminate:{user_id}:{term_ref}"
            admin_update.callback_query.answer = AsyncMock()
            admin_update.callback_query.edit_message_text = AsyncMock()
            admin_update.effective_user.id = 123456789
            admin_update.effective_user.full_name = "Admin Bob"

            loop.run_until_complete(admin_confirm_terminate(admin_update, self.context))

            # CRITICAL: User must NOT be deleted from DB; investor_status must be inactive
            user_doc = loop.run_until_complete(self.db.users.find_one({"telegram_id": user_id}))
            self.assertIsNotNone(user_doc, "User was deleted! Contract termination must NOT delete user.")
            self.assertEqual(user_doc["investor_status"], "inactive")
        finally:
            loop.close()

    def test_subscription_expiry_job(self):
        """Test subscription expiry check: warning for <= 4 days and expiration update for <= 0 days."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            now = utc_now()
            # 1. Sub expiring in 3 days (within 4-day window)
            sub_near = {
                "telegram_id": 111111,
                "telegram_username": "trader_near",
                "reference": "BM-20260928-1111",
                "service": "crypto",
                "service_name": "Crypto Futures",
                "status": "active",
                "expires_at": now + timedelta(days=3),
                "expiry_warning_sent": False,
            }
            # 2. Sub expired yesterday
            sub_expired = {
                "telegram_id": 222222,
                "telegram_username": "trader_expired",
                "reference": "BM-20260928-2222",
                "service": "forex_live",
                "service_name": "Forex Live",
                "status": "active",
                "expires_at": now - timedelta(days=1),
                "expiry_warning_sent": True,
            }

            loop.run_until_complete(self.db.subscriptions.insert_one(sub_near))
            loop.run_until_complete(self.db.subscriptions.insert_one(sub_expired))
            loop.run_until_complete(
                self.db.users.insert_one({
                    "telegram_id": 222222,
                    "subscriptions": {"forex_live": {"status": "active"}},
                })
            )

            # Run expiry check
            warned, expired = loop.run_until_complete(run_subscription_expiry_check(self.context.application))
            self.assertEqual(warned, 1)
            self.assertEqual(expired, 1)

            # Check sub_near now has expiry_warning_sent = True
            doc_near = loop.run_until_complete(self.db.subscriptions.find_one({"telegram_id": 111111}))
            self.assertTrue(doc_near["expiry_warning_sent"])
            self.assertEqual(doc_near["status"], "active")

            # Check sub_expired now has status = "expired"
            doc_exp = loop.run_until_complete(self.db.subscriptions.find_one({"telegram_id": 222222}))
            self.assertEqual(doc_exp["status"], "expired")

            # Check user doc has updated subscription status = "expired"
            user_exp = loop.run_until_complete(self.db.users.find_one({"telegram_id": 222222}))
            self.assertEqual(user_exp["subscriptions"]["forex_live"]["status"], "expired")
        finally:
            loop.close()

    def test_payment_method_service_restrictions(self):
        """Verify Private Investment is strictly crypto and Naira is only available for Forex."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.context.user_data = {}
            update = MagicMock()
            update.callback_query = MagicMock()
            update.callback_query.edit_message_text = AsyncMock()
            update.callback_query.answer = AsyncMock()

            # Test 1: Private investment goes straight to crypto payment details
            self.context.user_data["registration"] = {
                "service": "private",
                "investment_amount": "500",
                "duration_key": "2m",
            }
            state = loop.run_until_complete(prompt_payment_method(update, self.context))
            self.assertEqual(state, PAYMENT_DETAILS)
            self.assertEqual(self.context.user_data["registration"]["payment_method"], "crypto")

            # Test 2: Forex prompts for payment method selection (Naira + Crypto)
            self.context.user_data["registration"] = {
                "service": "forex_live",
                "duration_key": "1m",
                "track": "live",
            }
            state = loop.run_until_complete(prompt_payment_method(update, self.context))
            self.assertEqual(state, SELECT_PAYMENT_METHOD)

            # Test 3: Receive payment method rejecting naira for non-forex
            self.context.user_data["registration"] = {
                "service": "private",
                "payment_info": {"service_name": "Private Investment", "amount": "500"},
            }
            update.callback_query.data = "paymethod:naira"
            state = loop.run_until_complete(receive_payment_method(update, self.context))
            self.assertEqual(state, SELECT_PAYMENT_METHOD)
            update.callback_query.answer.assert_called_with(
                "Naira bank transfer is only available for Forex subscriptions.", show_alert=True
            )
        finally:
            loop.close()

    def test_trading_referral_tier_calculation(self):
        """Verify dynamic trading referral tiers based on verified paid referrals count."""
        # Base: 0-4 paid refs -> 7%
        rate, current_min, next_min = get_trading_referral_tier(0)
        self.assertEqual(rate, Decimal("7"))
        self.assertEqual(current_min, 0)
        self.assertEqual(next_min, 5)

        rate, current_min, next_min = get_trading_referral_tier(4)
        self.assertEqual(rate, Decimal("7"))
        self.assertEqual(next_min, 5)

        # Tier 1: 5-14 paid refs -> 10%
        rate, current_min, next_min = get_trading_referral_tier(5)
        self.assertEqual(rate, Decimal("10"))
        self.assertEqual(current_min, 5)
        self.assertEqual(next_min, 15)

        rate, current_min, next_min = get_trading_referral_tier(14)
        self.assertEqual(rate, Decimal("10"))

        # Tier 2: 15-24 paid refs -> 12%
        rate, current_min, next_min = get_trading_referral_tier(15)
        self.assertEqual(rate, Decimal("12"))
        self.assertEqual(current_min, 15)
        self.assertEqual(next_min, 25)

        # Tier 3: 25-49 paid refs -> 15%
        rate, current_min, next_min = get_trading_referral_tier(25)
        self.assertEqual(rate, Decimal("15"))
        self.assertEqual(current_min, 25)
        self.assertEqual(next_min, 50)

        # Tier 4: 50-99 paid refs -> 20%
        rate, current_min, next_min = get_trading_referral_tier(50)
        self.assertEqual(rate, Decimal("20"))
        self.assertEqual(current_min, 50)
        self.assertEqual(next_min, 100)

        # Tier 5: 100+ paid refs -> 25%
        rate, current_min, next_min = get_trading_referral_tier(100)
        self.assertEqual(rate, Decimal("25"))
        self.assertEqual(current_min, 100)
        self.assertIsNone(next_min)

        rate, _, next_min = get_trading_referral_tier(500)
        self.assertEqual(rate, Decimal("25"))
        self.assertIsNone(next_min)

    def test_process_referral_on_payment_verified(self):
        """Verify automated commission calculation, paid conversion, and tier progression."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            referrer_id = 999001
            referred_user_id = 999002

            # Seed referrer
            loop.run_until_complete(
                self.db.users.insert_one({
                    "telegram_id": referrer_id,
                    "referral_id": "REFTEST999",
                    "username": "referrer_user",
                })
            )

            # Seed referred user attributed to referrer
            loop.run_until_complete(
                self.db.users.insert_one({
                    "telegram_id": referred_user_id,
                    "referred_by": referrer_id,
                    "is_paid_referral": False,
                })
            )

            # 1. First payment for Crypto Futures: $100 USD
            submission = {
                "reference": "BM-20260930-TEST0001",
                "telegram_id": referred_user_id,
                "service": "crypto",
                "service_name": "Crypto Futures Trading",
                "amount": "100",
                "currency": "USD",
            }
            com_doc = loop.run_until_complete(
                process_referral_on_payment_verified(self.context, submission)
            )

            # User should now be marked as paid referral
            u_doc = loop.run_until_complete(self.db.users.find_one({"telegram_id": referred_user_id}))
            self.assertTrue(u_doc["is_paid_referral"])

            # Referrer has 1 paid referral -> Base rate 7% -> Share = $7.00
            self.assertIsNotNone(com_doc)
            self.assertEqual(com_doc["commission_rate"], "7")
            self.assertEqual(com_doc["referrer_share"], "7.00")
            self.assertEqual(com_doc["program"], "trading_subscriptions")

            # 2. Second payment from same user ($149.9 for 3m)
            submission2 = {
                "reference": "BM-20260930-TEST0002",
                "telegram_id": referred_user_id,
                "service": "crypto",
                "service_name": "Crypto Futures Trading",
                "amount": "149.9",
                "currency": "USD",
            }
            com_doc2 = loop.run_until_complete(
                process_referral_on_payment_verified(self.context, submission2)
            )
            # Unique paid count is still 1 -> Base rate 7% -> 7% of 149.9 = 10.49
            self.assertEqual(com_doc2["commission_rate"], "7")
            self.assertEqual(com_doc2["referrer_share"], "10.49")

            # 3. Simulate adding 4 more distinct paid referrals for referrer (total = 5 paid referrals)
            for i in range(3, 7):
                loop.run_until_complete(
                    self.db.users.insert_one({
                        "telegram_id": 999000 + i,
                        "referred_by": referrer_id,
                        "is_paid_referral": True,
                    })
                )

            # Referrer now has 5 paid referrals -> unlocks Tier 1 (10%)
            submission3 = {
                "reference": "BM-20260930-TEST0003",
                "telegram_id": 999003,
                "service": "forex_live",
                "service_name": "Forex Live Trading",
                "amount": "200",
                "currency": "USD",
            }
            com_doc3 = loop.run_until_complete(
                process_referral_on_payment_verified(self.context, submission3)
            )
            self.assertEqual(com_doc3["commission_rate"], "10")
            self.assertEqual(com_doc3["referrer_share"], "20.00")
        finally:
            loop.close()

    def test_private_investment_profit_commission(self):
        """Verify Private Investment 10% profit commission crediting via admin command."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            referrer_id = 888001
            investor_id = 888002

            # Seed referrer and investor
            loop.run_until_complete(
                self.db.users.insert_one({
                    "telegram_id": referrer_id,
                    "referral_id": "REFINV888",
                })
            )
            loop.run_until_complete(
                self.db.users.insert_one({
                    "telegram_id": investor_id,
                    "referred_by": referrer_id,
                })
            )

            update = MagicMock()
            update.effective_user.id = 123456789  # admin
            update.effective_message.reply_text = AsyncMock()
            self.context.args = [str(investor_id), "500", "Month 1 profit"]

            loop.run_until_complete(admin_add_investment_profit(update, self.context))

            # Check commission record in database
            com = loop.run_until_complete(
                self.db.commissions.find_one({"referred_telegram_id": investor_id})
            )
            self.assertIsNotNone(com)
            self.assertEqual(com["program"], "private_investment")
            self.assertEqual(com["profit_amount"], "500")
            self.assertEqual(com["commission_rate"], "10")
            self.assertEqual(com["referrer_share"], "50.00")
            self.assertEqual(com["currency"], "USDT")
        finally:
            loop.close()

    def test_show_referral_and_tiers_screens(self):
        """Verify rendering of upgraded /referral dashboard and tiers schedule."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            user_id = 777001
            loop.run_until_complete(
                self.db.users.insert_one({
                    "telegram_id": user_id,
                    "referral_id": "REFSHOW777",
                })
            )

            update = MagicMock()
            update.effective_user.id = user_id
            update.effective_message.reply_text = AsyncMock()
            update.callback_query = None

            # Test show_referral
            loop.run_until_complete(show_referral(update, self.context))
            call_kwargs = update.effective_message.reply_text.call_args.kwargs
            sent_text = call_kwargs.get("text") or update.effective_message.reply_text.call_args[0][0]
            self.assertIn("PAWNS PARTNER & REFERRAL PROGRAM", sent_text)
            self.assertIn("Trading Subscriptions (Crypto Futures & Forex)", sent_text)
            self.assertIn("Private Investment", sent_text)
            self.assertIn("Qualified Paid Referrals:", sent_text)

            # Test show_referral_tiers
            update.effective_message.reply_text.reset_mock()
            loop.run_until_complete(show_referral_tiers(update, self.context))
            tiers_kwargs = update.effective_message.reply_text.call_args.kwargs
            tiers_text = tiers_kwargs.get("text") or update.effective_message.reply_text.call_args[0][0]
            self.assertIn("PAWNS REFERRAL TIER SCHEDULE", tiers_text)
            self.assertIn("Base Tier (0 – 4 paid refs):", tiers_text)
            self.assertIn("Tier 5 (100+ paid refs):", tiers_text)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
