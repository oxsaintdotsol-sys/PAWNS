# PAWNS Telegram Bot

PAWNS is an enterprise-grade Telegram bot platform engineered for investment management, multi-market trading onboarding, automated on-chain payment verification, and a high-conversion tiered referral ecosystem.

The system is built on Python 3.11+ using `python-telegram-bot` (v22.8) and PyMongo's asynchronous driver, providing end-to-end asynchronous concurrency, resilient MongoDB persistence, and native blockchain RPC integrations.

---

## Table of Contents

1. [System Capabilities Overview](#1-system-capabilities-overview)
2. [What a User Can Do](#2-what-a-user-can-do)
3. [What an Admin Can Do](#3-what-an-admin-can-do)
4. [Automated System & Backend Features](#4-automated-system--backend-features)
5. [Tiered Referral Engine](#5-tiered-referral-engine)
6. [Blockchain Verification Engine](#6-blockchain-verification-engine)
7. [Deployment Guide (Railway & Local)](#7-deployment-guide-railway--local)
8. [Environment Variables Reference](#8-environment-variables-reference)
9. [Running Tests](#9-running-tests)
10. [Database Architecture & Audit Trail](#10-database-architecture--audit-trail)

---

## 1. System Capabilities Overview

| Pillar | Capabilities |
| :--- | :--- |
| **Trading Services** | Crypto Futures (BingX partner or standard subscription), Forex (Live brokers & Prop firms), Synthetic Indices. |
| **Private Investment** | Crypto-only (USDT on TRON TRC20), minimum entry tier ($500), legal risk acknowledgments, withdrawal and termination request workflows. |
| **Payment Verification** | Automated on-chain verification (BSC BEP20 & TRON TRC20) via public RPCs/explorers; Naira bank transfer receipt upload for Forex. |
| **Referral Program** | Dual-track referral system: dynamic 6-tier progression (7% up to 25%) based strictly on paid trading subscriptions, plus a flat 10% profit-sharing commission for Private Investments. |
| **Admin Operations** | Real-time DM alerts with instant 1-click Verify/Reject inline buttons, dynamic link/wallet/fee management in MongoDB, stats analytics, and investment profit distribution. |
| **Architecture** | Async non-blocking event loop, atomic database operations, dual-mode (polling or webhook), and background subscription lifecycle jobs. |

---

## 2. What a User Can Do

### 🧭 Navigation & Information
- **Access Interactive Main Menu (`/start`, `/menu`)**: Easy-to-use inline keyboards for quick navigation between services and user account tools.
- **Company & Risk Information**: View detailed descriptions of PAWNS services (`/about`), read complete in-bot Terms of Service and Risk Disclosures (`/terms` with one-click navigation to Private Investment Terms), or connect directly with administrator support (`/support` routing directly to Telegram DM `@Moyin_13`).
- **Identity & Role Check (`/id`, `/myid`, `/whoami`)**: Check your Telegram ID, username, and active administrative status.

### 📈 Trading Onboarding
- **Crypto Futures**:
  - **BingX Partner Track**: Register using the official PAWNS BingX partner link, submit your BingX UID, and gain complimentary VIP trading access upon admin verification.
  - **Other Exchanges Track**: Choose between 1-Month ($100) or 3-Month ($149.9) subscription plans.
- **Forex (Live Brokers & Prop Firms)**:
  - **Live Brokers**: View recommended partner brokers (Exness and HFM) with direct registration links.
  - **Prop Firms**: Explore partnered prop firms (Naira Trader and Naira Prop) with custom affiliate links.
  - **Flexible Payment Methods**: Pay via Crypto (USDT on BSC/Tron) or Nigerian Naira (Bank Transfer) at live-configured exchange rates.
- **Synthetic Indices**:
  - Step-by-step onboarding for Deriv synthetic trading signals.

### 💼 Private Investment Portal
- **Strictly Crypto-Only**: Clean and secure USDT investment on Tron (TRC20).
- **Investor Protections**: Review investment agreements, legal terms, return definitions, and risk disclosures.
- **Investor Portal Actions**:
  - Submit profit withdrawal requests (`/withdraw`).
  - Request formal contract termination.

### 💳 Payment Submission & Verification
- **Automated Crypto Payment Verification**:
  - Submit your transaction hash (TXID) directly into the chat.
  - Instant automated on-chain verification checks transaction status, sender/receiver addresses, and token amounts on BSC or TRON.
  - Anti-replay protection prevents re-use of previously submitted TXIDs.
- **Naira Bank Transfer (Forex Only)**:
  - Upload payment proof as an image (JPG, PNG, WEBP) or document (PDF) with a reference code.
- **Tracking & Real-Time Alerts**:
  - Receive a unique reference code (`BM-YYYYMMDD-XXXXXXXX`).
  - Receive automated status notifications in Telegram when administrators verify, reject, or request additional information.

### 🎁 Tiered Referral Dashboard (`/referral`)
- **Generate Unique Referral Links**: Deep link formatted as `https://t.me/<bot_username>?start=ref_<referral_id>`.
- **Live Performance Dashboard**:
  - Track total attributed registrations vs **qualified paid referrals**.
  - View current commission rate percentage.
  - Interactive visual progress bar towards unlocking the next commission tier.
  - Detailed breakdown of total earned commissions (Trading Subscriptions vs Investment Profits).
- **Tier Schedule View (`/referral:tiers`)**: Inspect full criteria for all 6 commission levels.

---

## 3. What an Admin Can Do

### 🔐 Dual-Source Administrator Detection
The system detects administrators through a hybrid architecture combining static bootstrap and dynamic database privileges:
1. **Bootstrap Admins (`ADMIN_CHAT_IDS`)**: Defined in the environment variables as a comma-separated list of numeric Telegram User IDs. These IDs have permanent super-admin access.
2. **Dynamic Database Admins (`users.is_admin = True`)**: Admins can be added and revoked on the fly via bot commands (`/addadmin`, `/removeadmin`) without restarting the bot or redeploying.
3. **Security Feedback**: If an unauthorized user attempts an admin command, the bot replies with their specific Telegram ID and instructions to request authorization. Admins can also inspect their identity anytime using `/id` or `/myid`.
4. **Admin Control Center**: Authorized admins see an extra **🛠 Admin Control Center** button on the main menu, or can access it via `/admin` / `/panel`.

### ⚡ Real-Time Admin DM Alerts
- **Instant Payment Notifications**: Admins receive comprehensive DMs immediately upon submission:
  - User details (full name, username, Telegram ID).
  - Selected service, plan duration, amount, and payment method.
  - Uploaded receipts or transaction hashes with clickable blockchain explorer URLs.
  - On-chain audit details (detected recipient wallet, detected amount, confirmation status).
- **1-Click Review Buttons**:
  - `Verify ✅`: Atomically marks submission as verified, activates user subscription, calculates and attributes referral commission to referrer, and sends congratulatory onboarding message with VIP channel link to the user.
  - `Reject ❌`: Rejects the payment and prompts the admin for an optional rejection reason sent to the user.
  - `Request Info ℹ️`: Asks the user for additional verification evidence.
- **BingX UID & Investor Reviews**:
  - 1-click approval or rejection of submitted BingX UIDs.
  - Review and approve/reject investor withdrawal and termination requests.

### 🛠️ Administrator Commands & Control Center

| Command | Usage | Description |
| :--- | :--- | :--- |
| `/admin` or `/panel` | `/admin` | Opens the interactive **Admin Control Center** (Stats, Pending Queue, Settings, Admin Roles, Audit Trail). |
| `/announcement` | `/announcement` | Initiates an official broadcast flow with structured preview and confirmation buttons before delivering to all bot users. |
| `/admins` | `/admins` | Lists all authorized administrators (both environment bootstrap and database roles). |
| `/addadmin` | `/addadmin <telegram_id>` | Dynamically grants administrator privileges to a Telegram user. |
| `/removeadmin` | `/removeadmin <telegram_id>` | Revokes dynamic administrator privileges from a user. |
| `/stats` | `/stats` | Displays live metrics: total registered users, active investors, pending/approved payments, conversion rates, and total referral commissions paid. |
| `/settings` | `/settings` | Displays current dynamic configuration stored in MongoDB (wallets, fees, rates, and active links). |
| `/addinvestmentprofit` | `/addinvestmentprofit <investor_id> <profit_amount> [note]` | Records an investment profit event for an investor. Automatically calculates the 10% referral commission, logs it in `db.commissions`, credits the referrer, and notifies them via DM. |
| `/addcommission` | `/addcommission <referrer_id> <amount> <currency> [note]` | Manually credits referral commission to any user. |
| `/setwallet` | `/setwallet <crypto\|forex\|investment> <address>` | Dynamically updates receiving wallet address without bot restarts. |
| `/setnetwork` | `/setnetwork <crypto\|forex\|investment> <network>` | Dynamically updates payment network (e.g. `BSC (BEP20)`, `Tron (TRC20)`). |
| `/setfee` | `/setfee <crypto\|forex_live\|forex_prop\|synthetic> <amount>` | Dynamically updates service subscription fee pricing. |
| `/setmininvest` | `/setmininvest <amount>` | Adjusts the minimum private investment threshold. |
| `/setnairarate` | `/setnairarate <rate>` | Updates the USD to NGN exchange rate for Naira bank payments. |
| `/setinstructions` | `/setinstructions <investment\|trading> <text>` | Updates payment instructions displayed to users. |
| `/setlink` | `/setlink <key> <url>` | Dynamically updates external links stored in MongoDB (`bingx`, `broker_1`, `broker_2`, `prop_1`, `prop_2`, `support`, etc.). |
| `/checkexpiry` | `/checkexpiry` | Manually triggers the subscription expiry check across all active subscribers. |
| `/audit` | `/audit [limit]` | Displays the most recent entries from the administrative audit log. |
| `/report` | `/report` | Generates a CSV data export of transactions and investor records. |

---

## 4. Automated System & Backend Features

1. **Non-Blocking Asynchronous Concurrency**:
   - Configured with `ApplicationBuilder().concurrent_updates(True)`.
   - Admin reviews, user payments, and menu clicks process concurrently without thread locking or blocking other users.
2. **Atomic Verification State Machine**:
   - Database submissions transition from `PENDING` to `VERIFIED` atomically (`{"reference": ref, "payment_status": "PENDING"}`).
   - If two administrators click verify simultaneously, only one succeeds and the other is informed gracefully.
3. **Automated Subscription Expiration Monitor**:
   - Built-in background JobQueue runs every 4 days to flag expired trading subscriptions and notify users about renewal options.
4. **In-Memory Fallback Storage**:
   - For fast local testing without MongoDB, the bot includes an in-memory storage fallback.

---

## 5. Tiered Referral Engine

The referral engine provides two specialized payout structures:

### A. Trading Subscriptions (Dynamic Paid Tiers)
Referral tier progression is based strictly on **qualified paid referrals** (users who have completed a verified payment), preventing affiliate spam:

| Tier Level | Paid Referrals Required | Commission Rate |
| :---: | :---: | :---: |
| **Base** | 0 – 4 | **7%** |
| **Tier 1** | 5 – 14 | **10%** |
| **Tier 2** | 15 – 24 | **12%** |
| **Tier 3** | 25 – 49 | **15%** |
| **Tier 4** | 50 – 99 | **20%** |
| **Tier 5** | 100+ | **25%** |

- When an attributed referee completes a payment, the referrer's total paid count increments.
- The referrer instantly earns commission at their current active tier rate.
- If the new payment advances the referrer to the next tier, they are congratulated via DM.

### B. Private Investment (Profit Sharing)
- Flat **10% commission** paid on the **profit** generated by the referred investor.
- Triggered seamlessly via the `/addinvestmentprofit` admin command.

---

## 6. Blockchain Verification Engine

Located in `chain_verifier.py`, the verification module runs in-process to validate crypto payments without third-party commercial APIs:

- **Binance Smart Chain (BEP20)**:
  - Queries public BSC JSON-RPC endpoints (`binance.llamarpc.com`, `bsc-dataseed1.binance.org`, `rpc.ankr.com/bsc`).
  - Decodes `eth_getTransactionReceipt` logs for USDT `Transfer(address,address,uint256)` event topics.
  - Validates recipient wallet and expected token amount (18 decimals).
- **TRON (TRC20)**:
  - Queries public TronScan REST API.
  - Validates TRC20 transfer contract calls, target wallet, and expected amount (6 decimals).
- **Graceful Fallback**:
  - If public RPCs experience network timeouts or rate limits, the system safely marks the submission as `manual_review_needed` so administrators can review the transaction manually on the block explorer.

---

## 7. Deployment Guide (Railway & Local)

### Deploying to Railway (Recommended)

Railway deploys the entire repository and automatically runs the background worker.

1. **Push your code to GitHub**.
2. **Create Project**:
   - Log into [Railway](https://railway.app).
   - Click **New Project** → **Deploy from GitHub repo** → Select this repository.
3. **Database**:
   - Click **+ New Service** → **Database** → **MongoDB** (or use an existing MongoDB Atlas connection string).
4. **Environment Variables**:
   - In Railway, open your service's **Variables** tab and set the required variables (refer to `.env.example`).
5. **Start Command**:
   - The repository includes a `Procfile` configured with `worker: python pawnstrading_bot.py`. Railway detects this automatically.

### Running Locally (Polling Mode)

1. **Clone the repository and enter the directory**:
   ```bash
   cd /path/to/PAWNS
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Configure environment**:
   ```bash
   cp .env.example .env.local
   # Edit .env.local with your BOT_TOKEN and ADMIN_CHAT_IDS
   ```

5. **Run the bot**:
   ```bash
   ./run.sh
   # Or directly:
   source .env.local && python pawnstrading_bot.py
   ```

---

## 8. Environment Variables Reference

See `.env.example` for a ready-to-copy template:

```bash
# Required Core Credentials
BOT_TOKEN="your_telegram_bot_token"
ADMIN_CHAT_IDS="123456789,987654321"
MONGODB_URI="mongodb+srv://user:password@cluster.mongodb.net/?appName=Cluster0"
MONGODB_DATABASE="pawnstrading"

# Runtime Mode
RUN_MODE="polling" # "polling" or "webhook"
LOG_LEVEL="INFO"

# Referral Security
REFERRAL_SECRET="random_32_character_hex_string"

# Receiving Wallets
TRADING_WALLET="0xYourEVMWalletAddress"
TRADING_NETWORK="BSC (BEP20)"
INVESTMENT_WALLET="TYourTronWalletAddress"
INVESTMENT_NETWORK="Tron (TRC20)"

# Naira Bank Payment Configuration (Forex Only)
USD_NGN_RATE="1400"
NAIRA_BANK_NAME="Zenith Bank"
NAIRA_ACCOUNT_NUMBER="1234567890"
NAIRA_ACCOUNT_NAME="PAWNS Trading Services"
```

---

## 9. Running Tests

The test suite covers conversation flows, payment verification, database models, and the tiered referral engine:

```bash
# Run all automated tests
.venv/bin/python -m unittest discover tests
```

---

## 10. Database Architecture & Audit Trail

MongoDB collections used by PAWNS:

- `db.users`: User profiles, registration metadata, referral attribution (`referred_by`, `referral_code`, `is_paid_referral`).
- `db.submissions`: Payment and onboarding submissions (`reference`, `payment_status`, `txid`, `chain_verification`, `service`, `amount`).
- `db.commissions`: Verified referral payout records linked to referring users.
- `db.audit_log`: Chronological ledger of all administrative decisions (`admin_id`, `action`, `reference`, `timestamp`).
- `db.settings`: Dynamic runtime overrides (wallet addresses, custom links, fee schedules, conversion rates).
- `db.investment_withdrawals`: Records of investor withdrawal and contract termination requests.
