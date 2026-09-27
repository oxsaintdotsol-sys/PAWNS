import asyncio
import unittest
from decimal import Decimal
from pawnstrading_bot import Database, Settings, InMemoryCollection


def make_test_settings(mongodb_uri: str = "") -> Settings:
    return Settings(
        bot_token="test:token",
        mongodb_uri=mongodb_uri,
        database_name="pawnstrading_test",
        admin_chat_ids=(123456,),
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
        payment_instructions_investment="Send exact USDT amount via TRC20 / TRON network only.",
        payment_instructions_trading="Send exact USD / USDT equivalent via BSC / BEP20 network only.",
        about_text="about",
        terms_text="terms",
        return_basis_text="return terms",
        private_investment_enabled=True,
        minimum_investment=Decimal("500"),
        commission_percent=Decimal("10"),
    )


class TestDatabaseSubsystem(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = make_test_settings("")
        self.db = Database(self.settings)

    async def asyncSetUp(self) -> None:
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_memory_fallback(self) -> None:
        """When no URI or unreachable URI is provided, DB initializes memory mode seamlessly."""
        self.assertTrue(self.db.is_memory_mode)
        self.assertIsInstance(self.db.users, InMemoryCollection)
        self.assertIsInstance(self.db.settings, InMemoryCollection)
        self.assertIsInstance(self.db.submissions, InMemoryCollection)
        self.assertIsInstance(self.db.audit, InMemoryCollection)

    async def test_dynamic_settings_and_audit(self) -> None:
        """Setting values must update store and produce audit logs."""
        # Initial setting
        audit1 = await self.db.set_setting("fee_crypto", "65", admin_id=999)
        self.assertEqual(audit1["old_value"], "")
        self.assertEqual(audit1["new_value"], "65")

        val = await self.db.get_setting("fee_crypto", "50")
        self.assertEqual(val, "65")

        # Second update
        audit2 = await self.db.set_setting("fee_crypto", "75", admin_id=999)
        self.assertEqual(audit2["old_value"], "65")
        self.assertEqual(audit2["new_value"], "75")

        val2 = await self.db.get_setting("fee_crypto", "50")
        self.assertEqual(val2, "75")

    async def test_audit_cursor_iteration(self) -> None:
        """Audit log query must support sorting, limiting, and async iteration."""
        for i in range(5):
            await self.db.set_setting(f"key_{i}", f"val_{i}", admin_id=100 + i)

        cursor = self.db.audit.find({}).sort("created_at", -1).limit(3)
        results = [doc async for doc in cursor]
        self.assertEqual(len(results), 3)

    async def test_uri_whitespace_and_quote_stripping(self) -> None:
        """Database __init__ must strip quotes and whitespace from MongoDB URI."""
        custom_settings = make_test_settings('  "mongodb://127.0.0.1:27017"  ')
        db = Database(custom_settings)
        # Check that the raw URI was cleaned and client attempted with clean URI
        self.assertFalse(db.is_memory_mode)
        if db.client:
            self.assertEqual(db.client.PORT, 27017)
        await db.close()


if __name__ == "__main__":
    unittest.main()
