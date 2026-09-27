"""Blockchain verification and validation utilities for PAWNS bot.

Supports TRC20 (TRON) and BSC (BEP20) networks for TXID syntax validation,
wallet format checks, and public explorer queries.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

LOGGER = logging.getLogger("PAWNS.ChainVerifier")

# Hex patterns
HEX_64_RE = re.compile(r"^[a-fA-F0-9]{64}$")
BSC_TXID_RE = re.compile(r"^(?:0x)?[a-fA-F0-9]{64}$")
TRON_TXID_RE = re.compile(r"^[a-fA-F0-9]{64}$")

# Wallet patterns
EVM_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
TRON_WALLET_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")

# Standard BEP20 USDT contract on Binance Smart Chain
BSC_USDT_CONTRACT = "0x55d398326f99059ff775485246999027b3197955".lower()
# Standard TRC20 USDT contract on TRON
TRON_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@dataclass
class ChainResult:
    status: str  # "auto_confirmed", "manual_review_needed", "mismatch", "not_found"
    details: str
    network: str
    txid: str
    explorer_url: str
    confirmations: int | None = None
    detected_to: str | None = None
    detected_amount: str | None = None


def normalize_txid(txid: str, network: str) -> str:
    cleaned = txid.strip()
    if "bsc" in network.lower() or "bep20" in network.lower():
        if not cleaned.lower().startswith("0x") and len(cleaned) == 64:
            return f"0x{cleaned.lower()}"
        return cleaned.lower()
    return cleaned


def validate_txid_format(txid: str, network: str) -> bool:
    cleaned = txid.strip()
    if not cleaned:
        return False
    net_lower = network.lower()
    if "trc" in net_lower or "tron" in net_lower:
        return bool(TRON_TXID_RE.match(cleaned))
    if "bsc" in net_lower or "bep" in net_lower:
        return bool(BSC_TXID_RE.match(cleaned))
    return bool(HEX_64_RE.match(cleaned.removeprefix("0x")))


def validate_wallet_format(address: str, network: str) -> bool:
    cleaned = address.strip()
    if not cleaned:
        return False
    net_lower = network.lower()
    if "trc" in net_lower or "tron" in net_lower:
        return bool(TRON_WALLET_RE.match(cleaned))
    if "bsc" in net_lower or "bep" in net_lower:
        return bool(EVM_WALLET_RE.match(cleaned))
    return bool(EVM_WALLET_RE.match(cleaned) or TRON_WALLET_RE.match(cleaned))


def get_explorer_url(txid: str, network: str) -> str:
    cleaned = normalize_txid(txid, network)
    net_lower = network.lower()
    if "trc" in net_lower or "tron" in net_lower:
        return f"https://tronscan.org/#/transaction/{cleaned}"
    return f"https://bscscan.com/tx/{cleaned}"


async def verify_on_chain(
    txid: str,
    network: str,
    expected_wallet: str,
    expected_amount: Decimal | None = None,
) -> ChainResult:
    """Attempt an automated on-chain check via public explorer APIs or RPCs.

    Falls back to 'manual_review_needed' if public APIs time out or are rate-limited,
    ensuring payments can always be reviewed by administrators.
    """
    normalized = normalize_txid(txid, network)
    explorer_url = get_explorer_url(normalized, network)
    net_lower = network.lower()

    if "trc" in net_lower or "tron" in net_lower:
        return await _verify_tron(normalized, expected_wallet, expected_amount, explorer_url)
    return await _verify_bsc(normalized, expected_wallet, expected_amount, explorer_url)


async def _verify_bsc(
    txid: str,
    expected_wallet: str,
    expected_amount: Decimal | None,
    explorer_url: str,
) -> ChainResult:
    clean_txid = txid if txid.startswith("0x") else f"0x{txid}"
    expected_wallet_lower = expected_wallet.lower().strip()

    # Query public BSC JSON-RPC (binance dataseed)
    rpc_urls = [
        "https://binance.llamarpc.com",
        "https://bsc-dataseed1.binance.org",
        "https://rpc.ankr.com/bsc",
    ]

    for rpc_url in rpc_urls:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getTransactionReceipt",
                    "params": [clean_txid],
                }
                resp = await client.post(rpc_url, json=payload)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                receipt = data.get("result")
                if not receipt:
                    # Transaction might be pending or not found
                    continue

                status_hex = receipt.get("status")
                if status_hex != "0x1":
                    return ChainResult(
                        status="mismatch",
                        details="Transaction found on BSC but status is FAILED / REVERTED.",
                        network="BSC / BEP20",
                        txid=clean_txid,
                        explorer_url=explorer_url,
                    )

                logs = receipt.get("logs", [])
                # Check BEP20 USDT Transfer events: Transfer(address,address,uint256)
                # topic[0] = 0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
                transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
                found_match = False
                detected_amt: Decimal | None = None
                detected_recipient = ""

                for log in logs:
                    topics = log.get("topics", [])
                    if len(topics) >= 3 and topics[0].lower() == transfer_topic:
                        # topics[2] is 'to' address (zero-padded 32 bytes)
                        to_hex = "0x" + topics[2][-40:].lower()
                        detected_recipient = to_hex
                        if to_hex == expected_wallet_lower:
                            found_match = True
                            data_hex = log.get("data", "0x")
                            try:
                                raw_val = int(data_hex, 16)
                                # USDT on BSC has 18 decimals
                                detected_amt = Decimal(raw_val) / Decimal("1000000000000000000")
                            except (ValueError, ArithmeticError):
                                pass
                            break

                # Also check direct BNB transfer if 'to' matches
                tx_to = (receipt.get("to") or "").lower()
                if tx_to == expected_wallet_lower and not found_match:
                    found_match = True

                if found_match:
                    details = "Transaction confirmed on BSC."
                    if detected_amt is not None:
                        details += f" Detected token amount: ~{detected_amt:.2f} USDT."
                    return ChainResult(
                        status="auto_confirmed",
                        details=details,
                        network="BSC / BEP20",
                        txid=clean_txid,
                        explorer_url=explorer_url,
                        detected_to=expected_wallet,
                        detected_amount=str(detected_amt) if detected_amt else None,
                    )
                else:
                    return ChainResult(
                        status="mismatch",
                        details=f"Transaction succeeded on BSC, but recipient ({detected_recipient or tx_to}) did not match PAWNS wallet.",
                        network="BSC / BEP20",
                        txid=clean_txid,
                        explorer_url=explorer_url,
                        detected_to=detected_recipient or tx_to,
                    )
        except Exception as exc:
            LOGGER.debug("BSC RPC check failed for %s: %s", rpc_url, exc)
            continue

    return ChainResult(
        status="manual_review_needed",
        details="Could not verify via public BSC RPC (check directly on explorer).",
        network="BSC / BEP20",
        txid=clean_txid,
        explorer_url=explorer_url,
    )


async def _verify_tron(
    txid: str,
    expected_wallet: str,
    expected_amount: Decimal | None,
    explorer_url: str,
) -> ChainResult:
    # Query TronScan public API
    api_url = f"https://apilist.tronscanapi.com/api/transaction-info?hash={txid}"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(api_url, headers={"User-Agent": "PAWNS-Bot/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                if not data or not data.get("hash"):
                    return ChainResult(
                        status="manual_review_needed",
                        details="Transaction not yet indexed by TronScan API. Verify on explorer.",
                        network="TRC20 / TRON",
                        txid=txid,
                        explorer_url=explorer_url,
                    )

                confirmed = data.get("confirmed", False)
                contract_ret = data.get("contractRet", "")
                if contract_ret and contract_ret != "SUCCESS":
                    return ChainResult(
                        status="mismatch",
                        details=f"Tron transaction status is not SUCCESS ({contract_ret}).",
                        network="TRC20 / TRON",
                        txid=txid,
                        explorer_url=explorer_url,
                    )

                # Check TRC20 transfer info
                trc20_transfers = data.get("trc20TransferInfo", [])
                match = False
                detected_amt: Decimal | None = None
                recipient = ""
                for transfer in trc20_transfers:
                    to_addr = transfer.get("to_address", "")
                    recipient = to_addr
                    if to_addr == expected_wallet:
                        match = True
                        try:
                            # USDT on TRC20 has 6 decimals
                            decimals = int(transfer.get("decimals", 6))
                            raw_val = int(transfer.get("amount_str", "0"))
                            detected_amt = Decimal(raw_val) / (Decimal(10) ** decimals)
                        except (ValueError, ArithmeticError):
                            pass
                        break

                if not match and data.get("toAddress") == expected_wallet:
                    match = True

                if match:
                    details = "Transaction confirmed on TRON."
                    if detected_amt is not None:
                        details += f" Detected TRC20 USDT: {detected_amt:.2f}."
                    return ChainResult(
                        status="auto_confirmed",
                        details=details,
                        network="TRC20 / TRON",
                        txid=txid,
                        explorer_url=explorer_url,
                        confirmations=1 if confirmed else 0,
                        detected_to=expected_wallet,
                        detected_amount=str(detected_amt) if detected_amt else None,
                    )
                else:
                    return ChainResult(
                        status="mismatch",
                        details=f"Transaction succeeded on TRON, but destination ({recipient or data.get('toAddress')}) does not match PAWNS wallet.",
                        network="TRC20 / TRON",
                        txid=txid,
                        explorer_url=explorer_url,
                    )
    except Exception as exc:
        LOGGER.debug("TronScan API check failed: %s", exc)

    return ChainResult(
        status="manual_review_needed",
        details="Could not verify via TronScan API. Verify directly on TronScan explorer.",
        network="TRC20 / TRON",
        txid=txid,
        explorer_url=explorer_url,
    )
