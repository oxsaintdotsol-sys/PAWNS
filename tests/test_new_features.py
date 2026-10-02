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
    is_admin_user,
    require_admin,
    cmd_my_id,
    admin_panel_menu,
    admin_add_admin,
    admin_remove_admin,
    admin_list_admins,
    show_support,
    show_about,
    show_terms,
    show_investment_terms,
    show_forex_live,
    show_forex_prop,
    main_menu_keyboard,
    format_announcement_message,
    admin_announcement_start,
    receive_announcement_text,
    admin_announcement_proceed,
    admin_announcement_cancel,
    ANNOUNCEMENT_TEXT_INPUT,
    ANNOUNCEMENT_CONFIRM,
    ADMIN_VERIFY_LINK_INPUT,
    ConversationHandler,
    USER_COMMANDS,
    ADMIN_COMMANDS,
    normalize_invite_link,
    complete_payment_verification,
    admin_review_verify_entry,
    receive_admin_verify_link,
    receive_admin_verify_default,
    receive_admin_verify_cancel,
)
from telegram.error import Forbidden


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
        self.context.user_data = {}
        self.context.bot.send_message = AsyncMock()
        self.context.application.bot.send_message = AsyncMock()

    def test_bingx_and_standard_pricing_tiers(self):
        """Verify BingX discounted pricing tiers and Standard pricing tiers."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # BingX tiers: 1m=$40, 3m=$90, 6m=$150, 12m=$200
            self.assertEqual(CRYPTO_BINGX_FEES["1m"], Decimal("40"))
            self.assertEqual(CRYPTO_BINGX_FEES["3m"], Decimal("90"))
            self.assertEqual(CRYPTO_BINGX_FEES["6m"], Decimal("150"))
            self.assertEqual(CRYPTO_BINGX_FEES["12m"], Decimal("200"))

            # Standard tiers: 1m=$70, 3m=$149.9, 6m=$250, 12m=$300
            self.assertEqual(CRYPTO_STANDARD_FEES["1m"], Decimal("70"))
            self.assertEqual(CRYPTO_STANDARD_FEES["3m"], Decimal("149.9"))
            self.assertEqual(CRYPTO_STANDARD_FEES["6m"], Decimal("250"))
            self.assertEqual(CRYPTO_STANDARD_FEES["12m"], Decimal("300"))

            # Forex tiers: 1m=$50, 3m=$70, 6m=$150, 12m=$200
            self.assertEqual(FOREX_FEES["1m"], Decimal("50"))
            self.assertEqual(FOREX_FEES["3m"], Decimal("70"))
            self.assertEqual(FOREX_FEES["6m"], Decimal("150"))
            self.assertEqual(FOREX_FEES["12m"], Decimal("200"))

            # Verify get_service_payment_info returns correct tier
            bingx_1m = loop.run_until_complete(
                get_service_payment_info(self.context, "crypto", duration="1m", track="bingx")
            )
            self.assertEqual(bingx_1m["amount"], "40")

            bingx_6m = loop.run_until_complete(
                get_service_payment_info(self.context, "crypto", duration="6m", track="bingx")
            )
            self.assertEqual(bingx_6m["amount"], "150")

            bingx_1yr = loop.run_until_complete(
                get_service_payment_info(self.context, "crypto", duration="12m", track="bingx")
            )
            self.assertEqual(bingx_1yr["amount"], "200")

            std_1m = loop.run_until_complete(
                get_service_payment_info(self.context, "crypto", duration="1m", track="standard")
            )
            self.assertEqual(std_1m["amount"], "70")

            forex_live_3m = loop.run_until_complete(
                get_service_payment_info(self.context, "forex_live", duration="3m")
            )
            self.assertEqual(forex_live_3m["amount"], "70")

            forex_prop_6m = loop.run_until_complete(
                get_service_payment_info(self.context, "forex_prop", duration="6m")
            )
            self.assertEqual(forex_prop_6m["amount"], "150")
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

    def test_admin_auth_and_management(self):
        """Verify dual-source admin detection (env + db), access control, /myid, and role management."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            admin_id = 123456789
            regular_user_id = 999888777
            new_admin_id = 555444333

            # 1. Verification of is_admin_user
            self.assertTrue(is_admin_user(admin_id, self.context))
            self.assertFalse(is_admin_user(regular_user_id, self.context))

            # 2. require_admin for non-admin responds with denied message containing ID
            non_admin_update = MagicMock()
            non_admin_update.effective_user.id = regular_user_id
            non_admin_update.callback_query = None
            non_admin_update.effective_message.reply_text = AsyncMock()

            result = loop.run_until_complete(require_admin(non_admin_update, self.context))
            self.assertFalse(result)
            denied_msg = non_admin_update.effective_message.reply_text.call_args[0][0]
            self.assertIn("Access Denied", denied_msg)
            self.assertIn(str(regular_user_id), denied_msg)
            self.assertIn(f"/addadmin {regular_user_id}", denied_msg)

            # 3. Add admin dynamically via admin_add_admin
            admin_update = MagicMock()
            admin_update.effective_user.id = admin_id
            admin_update.effective_message.reply_text = AsyncMock()
            self.context.args = [str(new_admin_id)]

            loop.run_until_complete(admin_add_admin(admin_update, self.context))
            # Now new_admin_id is admin
            self.assertTrue(is_admin_user(new_admin_id, self.context))

            # 4. Check /id command for user
            id_update = MagicMock()
            id_update.effective_user.id = new_admin_id
            id_update.effective_user.first_name = "New"
            id_update.effective_user.last_name = "Admin"
            id_update.effective_user.username = "new_admin_user"
            id_update.effective_message.reply_text = AsyncMock()

            loop.run_until_complete(cmd_my_id(id_update, self.context))
            id_text = id_update.effective_message.reply_text.call_args[0][0]
            self.assertIn(str(new_admin_id), id_text)
            self.assertIn("Administrator", id_text)

            # 5. admin_remove_admin cannot remove bootstrap env admin
            self.context.args = [str(admin_id)]
            admin_update.effective_message.reply_text.reset_mock()
            loop.run_until_complete(admin_remove_admin(admin_update, self.context))
            cant_remove_msg = admin_update.effective_message.reply_text.call_args[0][0]
            self.assertIn("Cannot revoke", cant_remove_msg)
            self.assertTrue(is_admin_user(admin_id, self.context))

            # 6. admin_remove_admin can remove database admin
            self.context.args = [str(new_admin_id)]
            admin_update.effective_message.reply_text.reset_mock()
            loop.run_until_complete(admin_remove_admin(admin_update, self.context))
            removed_msg = admin_update.effective_message.reply_text.call_args[0][0]
            self.assertIn("Admin Removed", removed_msg)
            self.assertFalse(is_admin_user(new_admin_id, self.context))

            # 7. Main menu keyboard admin button visibility
            kb_user = main_menu_keyboard(is_admin_user=False)
            user_buttons = [btn.text for row in kb_user.inline_keyboard for btn in row]
            self.assertNotIn("🛠 Admin Control Center", user_buttons)

            kb_admin = main_menu_keyboard(is_admin_user=True)
            admin_buttons = [btn.text for row in kb_admin.inline_keyboard for btn in row]
            self.assertIn("🛠 Admin Control Center", admin_buttons)

            # 8. Role-scoped slash command isolation
            user_cmd_names = [cmd.command for cmd in USER_COMMANDS]
            self.assertNotIn("admin", user_cmd_names)
            self.assertNotIn("stats", user_cmd_names)
            self.assertNotIn("announcement", user_cmd_names)
            self.assertNotIn("addadmin", user_cmd_names)
            self.assertNotIn("removeadmin", user_cmd_names)
            self.assertIn("start", user_cmd_names)
            self.assertIn("menu", user_cmd_names)
            self.assertIn("support", user_cmd_names)

            admin_cmd_names = [cmd.command for cmd in ADMIN_COMMANDS]
            self.assertIn("admin", admin_cmd_names)
            self.assertIn("stats", admin_cmd_names)
            self.assertIn("announcement", admin_cmd_names)
            self.assertIn("addadmin", admin_cmd_names)
            self.assertIn("removeadmin", admin_cmd_names)
        finally:
            loop.close()

    def test_support_routing_and_in_bot_terms(self):
        """Verify support DM routing to @Moyin_13 and full in-bot legal terms display."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            update = MagicMock()
            update.effective_user.id = 999111
            update.callback_query = None
            update.effective_message.reply_text = AsyncMock()

            # Support routing
            loop.run_until_complete(show_support(update, self.context))
            call_kwargs = update.effective_message.reply_text.call_args.kwargs
            sent_text = call_kwargs.get("text") or update.effective_message.reply_text.call_args[0][0]
            kb = call_kwargs.get("reply_markup") or update.effective_message.reply_text.call_args[0][1]
            self.assertIn("@Moyin_13", sent_text)
            support_url = kb.inline_keyboard[0][0].url
            self.assertEqual(support_url, "https://t.me/Moyin_13")

            # General terms of service & risk disclosure
            update.effective_message.reply_text.reset_mock()
            loop.run_until_complete(show_terms(update, self.context))
            call_kwargs = update.effective_message.reply_text.call_args.kwargs
            terms_text = call_kwargs.get("text") or update.effective_message.reply_text.call_args[0][0]
            self.assertIn("PAWNS TERMS OF SERVICE &amp; RISK DISCLOSURE", terms_text)
            self.assertIn("Financial Risk &amp; Leverage Warning", terms_text)
            self.assertIn("No Personalized Financial Advice", terms_text)
            terms_kb = call_kwargs.get("reply_markup") or update.effective_message.reply_text.call_args[0][1]
            terms_btn_cbs = [btn.callback_data for row in terms_kb.inline_keyboard for btn in row if btn.callback_data]
            self.assertIn("terms:investment", terms_btn_cbs)
            self.assertIn("menu", terms_btn_cbs)

            # Private investment terms
            update.effective_message.reply_text.reset_mock()
            loop.run_until_complete(show_investment_terms(update, self.context))
            call_kwargs = update.effective_message.reply_text.call_args.kwargs
            inv_terms_text = call_kwargs.get("text") or update.effective_message.reply_text.call_args[0][0]
            self.assertIn("PAWNS PRIVATE INVESTMENT TERMS &amp; RISK POLICY", inv_terms_text)
            self.assertIn("Minimum Allocation &amp; Commitments", inv_terms_text)
            self.assertIn("Profit Distribution &amp; Return Basis", inv_terms_text)
        finally:
            loop.close()

    def test_disabled_third_broker_and_prop(self):
        """Verify broker 3 and prop 3 are disabled/excluded from live selection."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            update = MagicMock()
            update.effective_user.id = 999111
            update.callback_query = None
            update.effective_message.reply_text = AsyncMock()

            # Forex Live account brokers
            loop.run_until_complete(show_forex_live(update, self.context))
            call_kwargs = update.effective_message.reply_text.call_args.kwargs
            kb_live = call_kwargs.get("reply_markup") or update.effective_message.reply_text.call_args[0][1]
            live_btn_labels = [btn.text for row in kb_live.inline_keyboard for btn in row]
            self.assertTrue(any("Exness" in lbl for lbl in live_btn_labels))
            self.assertTrue(any("HFM" in lbl for lbl in live_btn_labels))
            self.assertFalse(any("Deriv" in lbl for lbl in live_btn_labels))

            # Forex Prop firm partners
            update.effective_message.reply_text.reset_mock()
            loop.run_until_complete(show_forex_prop(update, self.context))
            call_kwargs = update.effective_message.reply_text.call_args.kwargs
            kb_prop = call_kwargs.get("reply_markup") or update.effective_message.reply_text.call_args[0][1]
            prop_btn_labels = [btn.text for row in kb_prop.inline_keyboard for btn in row]
            self.assertTrue(any("Naira Trader" in lbl for lbl in prop_btn_labels))
            self.assertTrue(any("Naira Prop" in lbl for lbl in prop_btn_labels))
            self.assertFalse(any("Global Dollar" in lbl for lbl in prop_btn_labels))
        finally:
            loop.close()

    def test_admin_announcement_workflow(self):
        """Verify the full /announcement admin workflow: preview, confirmation, and broadcast."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            admin_id = 123456789
            non_admin_id = 888777666
            self.context.user_data = {}

            # Seed 3 users in database
            for uid in [1001, 1002, 1003]:
                loop.run_until_complete(
                    self.db.users.update_one(
                        {"telegram_id": uid},
                        {"$set": {"full_name": f"User {uid}", "telegram_id": uid}},
                        upsert=True,
                    )
                )

            # 1. Non-admin is denied
            non_admin_update = MagicMock()
            non_admin_update.effective_user.id = non_admin_id
            non_admin_update.callback_query = None
            non_admin_update.effective_message.reply_text = AsyncMock()

            state = loop.run_until_complete(admin_announcement_start(non_admin_update, self.context))
            self.assertEqual(state, ConversationHandler.END)

            # 2. Admin initiates /announcement
            admin_update = MagicMock()
            admin_update.effective_user.id = admin_id
            admin_update.callback_query = None
            admin_update.effective_message.reply_text = AsyncMock()

            state = loop.run_until_complete(admin_announcement_start(admin_update, self.context))
            self.assertEqual(state, ANNOUNCEMENT_TEXT_INPUT)
            prompt_text = admin_update.effective_message.reply_text.call_args[0][0]
            self.assertIn("PAWNS BROADCAST ANNOUNCEMENT", prompt_text)
            self.assertIn("registered bot user(s)", prompt_text)

            # 3. Admin sends empty text
            empty_msg_update = MagicMock()
            empty_msg_update.effective_user.id = admin_id
            empty_msg_update.effective_message.text = "   "
            empty_msg_update.effective_message.text_html = ""
            empty_msg_update.effective_message.reply_text = AsyncMock()

            state = loop.run_until_complete(receive_announcement_text(empty_msg_update, self.context))
            self.assertEqual(state, ANNOUNCEMENT_TEXT_INPUT)

            # 4. Admin sends valid text -> receives preview
            text_update = MagicMock()
            text_update.effective_user.id = admin_id
            text_update.effective_message.text = "Exciting VIP trading strategy update!"
            text_update.effective_message.text_html = "<b>Exciting VIP</b> trading strategy update!"
            text_update.effective_message.reply_text = AsyncMock()

            state = loop.run_until_complete(receive_announcement_text(text_update, self.context))
            self.assertEqual(state, ANNOUNCEMENT_CONFIRM)

            preview_kwargs = text_update.effective_message.reply_text.call_args.kwargs
            preview_text = text_update.effective_message.reply_text.call_args[0][0]
            kb = preview_kwargs.get("reply_markup") or text_update.effective_message.reply_text.call_args[0][1]

            self.assertIn("ANNOUNCEMENT PREVIEW", preview_text)
            self.assertIn("🔊 <b>PAWNS ANNOUNCEMENT</b> 🔊", preview_text)
            self.assertIn("<b>Exciting VIP</b> trading strategy update!", preview_text)
            btn_cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
            self.assertIn("announce:proceed", btn_cbs)
            self.assertIn("announce:cancel", btn_cbs)

            # 5. Cancellation test
            cancel_update = MagicMock()
            cancel_update.effective_user.id = admin_id
            cancel_update.callback_query = MagicMock()
            cancel_update.callback_query.answer = AsyncMock()
            cancel_update.callback_query.edit_message_text = AsyncMock()

            cancel_state = loop.run_until_complete(admin_announcement_cancel(cancel_update, self.context))
            self.assertEqual(cancel_state, ConversationHandler.END)
            self.assertNotIn("announcement_content", self.context.user_data)
            cancel_text = cancel_update.callback_query.edit_message_text.call_args[0][0]
            self.assertIn("Announcement cancelled", cancel_text)

            # 6. Proceed & Broadcast test (2 successful, 1 blocked)
            self.context.user_data["announcement_content"] = "Live Broadcast Content"

            proceed_update = MagicMock()
            proceed_update.effective_user.id = admin_id
            proceed_update.effective_user.username = "head_admin"
            proceed_update.callback_query = MagicMock()
            proceed_update.callback_query.answer = AsyncMock()
            proceed_update.callback_query.edit_message_text = AsyncMock()

            async def mock_send_message(chat_id, **kwargs):
                if chat_id == 1003:
                    raise Forbidden("Bot was blocked by the user")
                return MagicMock()

            self.context.bot.send_message = AsyncMock(side_effect=mock_send_message)

            proceed_state = loop.run_until_complete(admin_announcement_proceed(proceed_update, self.context))
            self.assertEqual(proceed_state, ConversationHandler.END)
            self.assertNotIn("announcement_content", self.context.user_data)

            # Verify audit trail
            audit_entry = loop.run_until_complete(
                self.db.audit.find_one({"action": "announcement_broadcast"})
            )
            self.assertIsNotNone(audit_entry)
            self.assertEqual(audit_entry["sent_count"], 2)
            self.assertEqual(audit_entry["failed_count"], 1)

            # Verify completion message
            final_report = proceed_update.callback_query.edit_message_text.call_args[0][0]
            self.assertIn("ANNOUNCEMENT BROADCAST COMPLETED", final_report)
            self.assertIn("Successfully Delivered:</b> 2", final_report)
            self.assertIn("Failed / Blocked:</b> 1", final_report)
        finally:
            loop.close()

    def test_normalize_invite_link(self):
        # Valid full invite links
        self.assertEqual(
            normalize_invite_link("https://t.me/+AbCdEf12345"),
            "https://t.me/+AbCdEf12345",
        )
        self.assertEqual(
            normalize_invite_link("http://t.me/joinchat/AbCdEf12345"),
            "http://t.me/joinchat/AbCdEf12345",
        )
        # Without https:// scheme
        self.assertEqual(
            normalize_invite_link("t.me/+AbCdEf12345"),
            "https://t.me/+AbCdEf12345",
        )
        # Starting with plus (+)
        self.assertEqual(
            normalize_invite_link("+AbCdEf12345"),
            "https://t.me/+AbCdEf12345",
        )
        # Invalid inputs
        self.assertIsNone(normalize_invite_link(""))
        self.assertIsNone(normalize_invite_link("   "))
        self.assertIsNone(normalize_invite_link("random text here"))
        self.assertIsNone(normalize_invite_link("notaurl"))

    def test_admin_verify_crypto_payment_dynamic_invite_link(self):
        loop = asyncio.new_event_loop()
        try:
            admin_id = 123456789
            user_id = 987654321
            reference = "BM-20260930-11223344"

            # 1. Seed pending crypto futures submission
            sub_doc = {
                "reference": reference,
                "telegram_id": user_id,
                "telegram_username": "cryptotrader",
                "service": "crypto",
                "service_name": "Crypto Futures Trading",
                "track": "standard",
                "duration_key": "1m",
                "amount": "70",
                "currency": "USDT",
                "payment_status": "PENDING",
                "created_at": utc_now(),
            }
            loop.run_until_complete(self.db.submissions.insert_one(sub_doc))

            # 2. Admin clicks Verify button -> entry point
            entry_update = MagicMock()
            entry_update.effective_user.id = admin_id
            entry_update.effective_chat.id = admin_id
            entry_update.callback_query = MagicMock()
            entry_update.callback_query.data = f"admin:verify:{reference}"
            entry_update.callback_query.answer = AsyncMock()
            entry_update.callback_query.message = MagicMock()
            entry_update.callback_query.message.message_id = 555
            entry_update.callback_query.message.photo = None
            entry_update.callback_query.message.document = None
            entry_update.callback_query.message.edit_text = AsyncMock()
            entry_update.callback_query.message.edit_caption = AsyncMock()

            prompt_msg = MagicMock()
            prompt_msg.message_id = 666
            self.context.bot.send_message = AsyncMock(return_value=prompt_msg)
            self.context.bot.delete_message = AsyncMock()

            state = loop.run_until_complete(admin_review_verify_entry(entry_update, self.context))
            self.assertEqual(state, ADMIN_VERIFY_LINK_INPUT)
            self.assertIn("admin_verify", self.context.user_data)
            self.assertEqual(self.context.user_data["admin_verify"]["reference"], reference)

            # Check prompt message content
            prompt_call = self.context.bot.send_message.call_args
            self.assertIn("Enter VIP Channel Invite Link", prompt_call[1]["text"])
            self.assertIn(reference, prompt_call[1]["text"])

            # 3. Admin enters invalid link text -> warned, stays in ADMIN_VERIFY_LINK_INPUT
            invalid_update = MagicMock()
            invalid_update.effective_user.id = admin_id
            invalid_update.message = MagicMock()
            invalid_update.message.text = "invalid text"
            invalid_update.message.reply_text = AsyncMock()

            invalid_state = loop.run_until_complete(receive_admin_verify_link(invalid_update, self.context))
            self.assertEqual(invalid_state, ADMIN_VERIFY_LINK_INPUT)
            self.assertIn("not appear to be a valid Telegram channel invite link", invalid_update.message.reply_text.call_args[0][0])

            # 4. Admin enters custom one-time link: https://t.me/+CryptoVIP_OneTime_123
            custom_link = "https://t.me/+CryptoVIP_OneTime_123"
            link_update = MagicMock()
            link_update.effective_user.id = admin_id
            link_update.effective_user.full_name = "Admin Alice"
            link_update.effective_chat.id = admin_id
            link_update.message = MagicMock()
            link_update.message.text = custom_link
            link_update.message.reply_text = AsyncMock()

            final_state = loop.run_until_complete(receive_admin_verify_link(link_update, self.context))
            self.assertEqual(final_state, ConversationHandler.END)
            self.assertNotIn("admin_verify", self.context.user_data)

            # Verify prompt message was deleted
            self.context.bot.delete_message.assert_called_with(chat_id=admin_id, message_id=666)

            # Verify admin received confirmation of link sent
            admin_confirm_text = link_update.message.reply_text.call_args[0][0]
            self.assertIn("Payment Verified & Invite Link Sent!", admin_confirm_text)
            self.assertIn(custom_link, admin_confirm_text)

            # Verify user received message with unique invite link in button
            user_send_calls = [
                call for call in self.context.bot.send_message.call_args_list
                if call[1].get("chat_id") == user_id
            ]
            self.assertEqual(len(user_send_calls), 1)
            user_call = user_send_calls[0]
            self.assertIn("Payment Status: VERIFIED ✅", user_call[1]["text"])
            self.assertIn("Join the VIP Channel", user_call[1]["text"])

            reply_markup = user_call[1]["reply_markup"]
            vip_button = reply_markup.inline_keyboard[0][0]
            self.assertEqual(vip_button.text, "🚀 Join VIP Trading Channel")
            self.assertEqual(vip_button.url, custom_link)

            # Verify DB updates
            updated_sub = loop.run_until_complete(self.db.submissions.find_one({"reference": reference}))
            self.assertEqual(updated_sub["payment_status"], "VERIFIED ✅")
            self.assertEqual(updated_sub["invite_link"], custom_link)

            subscription = loop.run_until_complete(self.db.subscriptions.find_one({"reference": reference}))
            self.assertIsNotNone(subscription)
            self.assertEqual(subscription["status"], "active")
            self.assertEqual(subscription["invite_link"], custom_link)
        finally:
            loop.close()

    def test_admin_verify_default_channel_fallback(self):
        loop = asyncio.new_event_loop()
        try:
            admin_id = 123456789
            user_id = 888777666
            reference = "BM-20260930-55667788"

            sub_doc = {
                "reference": reference,
                "telegram_id": user_id,
                "service": "forex_live",
                "service_name": "Forex Live Account Trading",
                "track": "standard",
                "duration_key": "1m",
                "amount": "50",
                "currency": "USDT",
                "payment_status": "PENDING",
                "created_at": utc_now(),
            }
            loop.run_until_complete(self.db.submissions.insert_one(sub_doc))

            # Set default channel in settings
            loop.run_until_complete(self.db.set_setting("pawns_channel", "https://t.me/pawns_default_channel", admin_id))

            entry_update = MagicMock()
            entry_update.effective_user.id = admin_id
            entry_update.effective_chat.id = admin_id
            entry_update.callback_query = MagicMock()
            entry_update.callback_query.data = f"admin:verify:{reference}"
            entry_update.callback_query.answer = AsyncMock()
            entry_update.callback_query.message = MagicMock()
            entry_update.callback_query.message.photo = None
            entry_update.callback_query.message.document = None
            entry_update.callback_query.message.edit_text = AsyncMock()
            entry_update.callback_query.message.edit_caption = AsyncMock()

            prompt_msg = MagicMock()
            prompt_msg.message_id = 777
            self.context.bot.send_message = AsyncMock(return_value=prompt_msg)

            state = loop.run_until_complete(admin_review_verify_entry(entry_update, self.context))
            self.assertEqual(state, ADMIN_VERIFY_LINK_INPUT)

            # Admin clicks Use Default Configured Channel Link
            default_query_update = MagicMock()
            default_query_update.effective_user.id = admin_id
            default_query_update.callback_query = MagicMock()
            default_query_update.callback_query.data = f"admin:verify_default:{reference}"
            default_query_update.callback_query.answer = AsyncMock()
            default_query_update.callback_query.edit_message_text = AsyncMock()

            def_state = loop.run_until_complete(receive_admin_verify_default(default_query_update, self.context))
            self.assertEqual(def_state, ConversationHandler.END)
            self.assertNotIn("admin_verify", self.context.user_data)

            # Verify user received message with default channel url
            user_send_calls = [
                call for call in self.context.bot.send_message.call_args_list
                if call[1].get("chat_id") == user_id
            ]
            self.assertEqual(len(user_send_calls), 1)
            vip_button = user_send_calls[0][1]["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(vip_button.url, "https://t.me/pawns_default_channel")
        finally:
            loop.close()

    def test_admin_verify_private_investment_direct(self):
        loop = asyncio.new_event_loop()
        try:
            admin_id = 123456789
            user_id = 333444555
            reference = "BM-20260930-99887766"

            sub_doc = {
                "reference": reference,
                "telegram_id": user_id,
                "service": "private",
                "service_name": "PAWNS Private Investment",
                "amount": "1000",
                "currency": "USDT",
                "payment_status": "PENDING",
                "created_at": utc_now(),
            }
            loop.run_until_complete(self.db.submissions.insert_one(sub_doc))

            entry_update = MagicMock()
            entry_update.effective_user.id = admin_id
            entry_update.effective_chat.id = admin_id
            entry_update.callback_query = MagicMock()
            entry_update.callback_query.data = f"admin:verify:{reference}"
            entry_update.callback_query.answer = AsyncMock()
            entry_update.callback_query.message = MagicMock()
            entry_update.callback_query.message.photo = None
            entry_update.callback_query.message.document = None
            entry_update.callback_query.message.edit_text = AsyncMock()
            entry_update.callback_query.message.edit_caption = AsyncMock()

            self.context.bot.send_message = AsyncMock()

            # Private investment verification should complete immediately without entering invite link
            state = loop.run_until_complete(admin_review_verify_entry(entry_update, self.context))
            self.assertEqual(state, ConversationHandler.END)
            self.assertNotIn("admin_verify", self.context.user_data)

            # User gets onboarding button
            user_send_calls = [
                call for call in self.context.bot.send_message.call_args_list
                if call[1].get("chat_id") == user_id
            ]
            self.assertEqual(len(user_send_calls), 1)
            onboard_button = user_send_calls[0][1]["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(onboard_button.text, "📝 Complete Onboarding")
            self.assertEqual(onboard_button.callback_data, f"onboard_start:{reference}")

            # DB updated
            updated_sub = loop.run_until_complete(self.db.submissions.find_one({"reference": reference}))
            self.assertEqual(updated_sub["payment_status"], "VERIFIED ✅")
        finally:
            loop.close()

    def test_admin_verify_cancel(self):
        loop = asyncio.new_event_loop()
        try:
            admin_id = 123456789
            reference = "BM-20260930-11112222"

            sub_doc = {
                "reference": reference,
                "telegram_id": 111,
                "service": "crypto",
                "service_name": "Crypto Futures Trading",
                "payment_status": "PENDING",
                "created_at": utc_now(),
            }
            loop.run_until_complete(self.db.submissions.insert_one(sub_doc))

            self.context.user_data["admin_verify"] = {"reference": reference}

            cancel_update = MagicMock()
            cancel_update.callback_query = MagicMock()
            cancel_update.callback_query.answer = AsyncMock()
            cancel_update.callback_query.edit_message_text = AsyncMock()

            state = loop.run_until_complete(receive_admin_verify_cancel(cancel_update, self.context))
            self.assertEqual(state, ConversationHandler.END)
            self.assertNotIn("admin_verify", self.context.user_data)

            # DB submission remains PENDING
            sub = loop.run_until_complete(self.db.submissions.find_one({"reference": reference}))
            self.assertEqual(sub["payment_status"], "PENDING")
        finally:
            loop.close()


    def test_show_about_with_official_channel_button(self):
        loop = asyncio.new_event_loop()
        try:
            update = MagicMock()
            update.callback_query = MagicMock()
            update.callback_query.answer = AsyncMock()
            update.callback_query.edit_message_text = AsyncMock()

            # Set pawns_channel url in settings
            loop.run_until_complete(
                self.db.set_setting("pawns_channel", "https://t.me/pawns_official_channel", 123456789)
            )

            loop.run_until_complete(show_about(update, self.context))

            update.callback_query.edit_message_text.assert_called_once()
            call_args = update.callback_query.edit_message_text.call_args
            text = call_args[1]["text"]
            markup = call_args[1]["reply_markup"]

            self.assertIn("ABOUT PAWNS", text)
            # Channel button should be above Back button
            self.assertEqual(len(markup.inline_keyboard), 2)
            channel_btn = markup.inline_keyboard[0][0]
            back_btn = markup.inline_keyboard[1][0]

            self.assertEqual(channel_btn.text, "📢 Join Official Channel")
            self.assertEqual(channel_btn.url, "https://t.me/pawns_official_channel")
            self.assertEqual(back_btn.text, "⬅️ Back")
            self.assertEqual(back_btn.callback_data, "menu")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()


