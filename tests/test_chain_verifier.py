"""Unit tests for chain verification logic."""

import asyncio
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from chain_verifier import (
    ChainResult,
    normalize_txid,
    validate_txid_format,
    validate_wallet_format,
    verify_on_chain,
)


class TestChainVerifier(unittest.TestCase):
    def test_normalization(self):
        tx_bare = "a" * 64
        self.assertEqual(normalize_txid(tx_bare, "BSC / BEP20"), f"0x{tx_bare}")
        self.assertEqual(normalize_txid(f"0x{tx_bare}", "BSC / BEP20"), f"0x{tx_bare}")
        self.assertEqual(normalize_txid(tx_bare, "TRC20 / TRON"), tx_bare)

    def test_format_validation(self):
        valid_hex = "f" * 64
        self.assertTrue(validate_txid_format(valid_hex, "TRON"))
        self.assertTrue(validate_txid_format("0x" + valid_hex, "BSC"))
        self.assertFalse(validate_txid_format("xyz", "BSC"))

    @patch("httpx.AsyncClient.post")
    def test_bsc_verification_mock(self, mock_post):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Mock BSC receipt response
            expected_wallet = "0x64df5b5dc83b78f2e8d0e22b33ee471848a89b67"
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            # Zero-padded 32-byte address in topic[2]
            to_topic = "0x" + "0" * 24 + expected_wallet[2:].lower()
            mock_resp.json.return_value = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "status": "0x1",
                    "logs": [
                        {
                            "topics": [
                                "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                "0x0000000000000000000000001111111111111111111111111111111111111111",
                                to_topic,
                            ],
                            # 50 * 10^18 in hex = 0x2b5e3af16b1880000
                            "data": "0x2b5e3af16b1880000",
                        }
                    ],
                },
            }
            mock_post.return_value = mock_resp

            res = loop.run_until_complete(
                verify_on_chain("0x" + "1" * 64, "BSC / BEP20", expected_wallet, Decimal("50"))
            )
            self.assertEqual(res.status, "auto_confirmed")
            self.assertIn("50.00", res.details)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
