# pawns Telegram Bot Backend

This repository contains a single-file Python backend for the pawns Telegram bot:

- `pawnstrading_bot.py` — the complete bot application and webhook/polling server
- `README.md` — local setup, configuration, testing, and troubleshooting instructions

The backend supports service navigation, guided registrations, MongoDB storage, referral attribution, administrator DM notifications, payment-reference review, and administrator approval or rejection.

Payment evidence is never treated as automatic proof of payment. Every completed registration is saved as **Under Review** and must be verified by an authorised administrator.

## 1. Technology used

- Python 3.11 or newer
- `python-telegram-bot` 22.8
- PyMongo's asynchronous MongoDB client
- MongoDB Community Edition or MongoDB Atlas
- Optional HTTPS tunnel for local webhook testing

The simplest local setup uses Telegram long polling. Long polling does not require a public domain, HTTPS certificate, open router port, or tunnelling service.

## 2. What the backend currently provides

- `/start` and inline-button menu navigation
- Private investment registration
- Crypto futures onboarding
- Live forex onboarding
- Prop-firm forex onboarding
- Synthetic trading onboarding
- Terms, support, and company-information screens
- User registration and consent records
- Unique submission references
- Payment-reference and receipt collection
- MongoDB persistence
- Administrator notifications through Telegram DMs
- Administrator Verify and Reject buttons
- Status notifications sent back to users
- Referral IDs and Telegram deep links
- Referral attribution and verified commission events
- Configurable registration, support, and terms links
- Polling mode for easy local testing
- Webhook mode for deployment or tunnel testing

The current build uses text and inline buttons. Service artwork can be connected later without changing the database model.

## 3. Files required

Put both files in the same folder:

```text
pawnstrading-bot/
├── pawnstrading_bot.py
└── README.md
```

The examples below use `~/Projects/pawnstrading-bot` as the project folder. Change this path if the files are stored elsewhere.

## 4. Prerequisites on macOS

You need:

1. A Telegram bot token from Telegram's official `@BotFather` account.
2. The numeric Telegram ID of each administrator who should receive registrations.
3. Python 3.11 or newer.
4. A local MongoDB server or MongoDB Atlas connection string.
5. The PAWNS payment instructions, business links, terms, and support contact.

### Check Python

Open Terminal and run:

```bash
python3 --version
```

If the result is Python 3.11 or newer, continue. If Python is missing, install Homebrew and Python:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python
python3 --version
```

If Homebrew was already installed, only `brew install python` is needed.

## 5. Create the Telegram bot

1. Open Telegram.
2. Find the official `@BotFather` account.
3. Send `/newbot`.
4. Enter the requested display name and bot username.
5. Copy the token provided by BotFather.

Treat the token like a password. Anyone who obtains it can control the bot. Do not put it in a public Git repository, screenshot, shared document, or support message.

If a token is exposed, use BotFather to revoke it and issue a replacement.

## 6. Find the administrator Telegram ID

The application requires at least one numeric administrator ID before it can start.

For a new bot, one way to retrieve it is:

1. Send any message to the new bot in Telegram, such as `/start`.
2. In Terminal, temporarily export the bot token:

```bash
read -s BOT_TOKEN
export BOT_TOKEN
```

Paste the token when prompted and press Return. The token will not be displayed while typing.

3. Request the bot's pending updates:

```bash
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates" | python3 -m json.tool
```

4. Look for a structure similar to:

```json
"chat": {
    "id": 123456789,
    "first_name": "Admin"
}
```

The number in `chat.id` is the administrator Telegram ID.

For multiple administrators, collect each numeric ID and separate them with commas:

```text
123456789,987654321
```

Each administrator should open the finished bot and press **Start** at least once. Telegram normally prevents a bot from initiating a conversation with someone who has never started it.

## 7. Install and start MongoDB locally

MongoDB Atlas can be used instead, but a local database is convenient for localhost testing.

### Install MongoDB with Homebrew

```bash
brew tap mongodb/brew
brew update
brew install mongodb-community@8.0
```

Start MongoDB as a background service:

```bash
brew services start mongodb-community@8.0
```

Confirm that it responds:

```bash
mongosh --eval 'db.runCommand({ ping: 1 })'
```

A successful result contains:

```text
ok: 1
```

The local connection string used by the bot will be:

```text
mongodb://127.0.0.1:27017
```

To see available MongoDB formula versions if `@8.0` is unavailable:

```bash
brew search mongodb-community
```

Install one of the supported versions shown by Homebrew and use the same version name with `brew services start`.

## 8. Create a Python virtual environment

Move to the project folder:

```bash
cd ~/Projects/pawns-bot
```

Create a virtual environment:

```bash
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

The Terminal prompt should now start with `(.venv)`.

Upgrade the installer and install the dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install "python-telegram-bot[webhooks]==22.8" "pymongo>=4.11,<5" dnspython
```

Confirm the imports:

```bash
python -c "import telegram, pymongo; print('Telegram:', telegram.__version__); print('PyMongo:', pymongo.version)"
```

## 9. Create the local configuration file

Create a file named `.env.local` in the project folder:

```bash
touch .env.local
chmod 600 .env.local
nano .env.local
```

Paste and edit the following example:

```bash
export BOT_TOKEN="PASTE_THE_BOTFATHER_TOKEN_HERE"
export MONGODB_URI="mongodb://127.0.0.1:27017"
export MONGODB_DATABASE="pawns"
export ADMIN_CHAT_IDS="123456789"

export RUN_MODE="polling"
export LOG_LEVEL="INFO"

export REFERRAL_SECRET="REPLACE_WITH_A_LONG_RANDOM_VALUE"
export REFERRAL_COMMISSION_PERCENT="10"

export ABOUT_TEXT="pawns provides investment, trading onboarding, and related financial services."
export SUPPORT_URL="https://t.me/REPLACE_WITH_SUPPORT_USERNAME"
export TERMS_URL="https://example.com/terms"
export INVESTMENT_TERMS_URL="https://example.com/investment-terms"

export BINGX_URL="https://example.com/bingx-registration"
export BROKER_URL="https://example.com/broker-registration"
export PROP_FIRM_URL="https://example.com/prop-firm-registration"
export SYNTHETIC_URL="https://example.com/synthetic-registration"

export PAYMENT_INSTRUCTIONS="Replace this with the approved payment method, beneficiary details, currency, network where applicable, and the reference users must submit."

export MINIMUM_INVESTMENT="5"
export PRIVATE_INVESTMENT_ENABLED="false"
export RETURN_BASIS_TEXT="The signed agreement defines whether stated figures represent gross profit, net profit, or total payout, including fees, loss conditions, payment timing, and treatment of principal."
```

Generate a stable random referral secret with:

```bash
openssl rand -hex 32
```

Copy the result into `REFERRAL_SECRET`. Keep this value unchanged after launch; changing it changes newly calculated referral IDs.

In Nano, save with `Control + O`, press Return, and exit with `Control + X`.

### Protect the configuration

If this folder will use Git, create or update `.gitignore`:

```bash
printf '.venv/\n.env.local\n.env.webhook\n__pycache__/\n*.pyc\n' >> .gitignore
```

Never commit `.env.local` because it contains the bot token and business configuration.

## 10. Load the configuration

Every new Terminal session must activate the virtual environment and load the environment variables:

```bash
cd ~/Projects/pawns-bot
source .venv/bin/activate
source .env.local
```

Confirm the important non-secret values:

```bash
printf 'Mode: %s\nDatabase: %s\nAdmins: %s\n' "$RUN_MODE" "$MONGODB_DATABASE" "$ADMIN_CHAT_IDS"
```

Do not print `BOT_TOKEN` in screenshots or shared Terminal output.

## 11. Check the Python file before starting

Run a syntax check:

```bash
python -m py_compile pawnstrading_bot.py
```

No output means the syntax check passed.

## 12. Start the bot locally using polling

Make sure MongoDB is running, then start the bot:

```bash
python pawnstrading_bot.py
```

Expected startup output should include a line similar to:

```text
Bot @your_bot_username connected; database indexes ready
```

Keep this Terminal window open while testing. Stop the bot with `Control + C`.

Polling still runs the backend on your Mac, but Telegram updates are collected through an outbound connection. A public localhost URL is not required.

## 13. Test the user journey

Open the bot in Telegram and test the following:

1. Send `/start`.
2. Confirm the main menu appears.
3. Open **Crypto Futures**, **Forex**, or **Synthetic Trading**.
4. Start a registration.
5. Enter a two-word full name.
6. Enter a test account ID, such as `TEST-UID-001`.
7. Review the registration summary.
8. Select **I agree and continue**.
9. Enter a test payment reference, such as `LOCAL-TEST-001`.
10. Confirm the user receives a registration reference and **Under Review** status.
11. Confirm the configured administrator receives a DM containing the registration.
12. Press **Verify** or **Reject** in the administrator message.
13. Confirm the user's Telegram account receives the resulting status notification.

Use test data only. Do not submit a real payment, real trading credentials, passwords, seed phrases, private keys, OTPs, or authentication codes.

### Test a receipt upload

Repeat the registration and, at the payment-reference step, upload a harmless JPG, PNG, WEBP, or PDF test file. Put a reference such as `LOCAL-RECEIPT-002` in the caption.

The administrator should receive both the registration summary and a copied version of the uploaded evidence.

## 14. Test private investment registration

Private investment is deliberately disabled by default.

Before enabling it, configure:

- The final investment agreement URL
- The exact definition of each percentage
- Whether figures mean gross profit, net profit, or total payout
- Fees and loss conditions
- Payment timing
- Treatment and return of principal
- Approved payment instructions
- Final risk disclosure

For local testing with approved test wording, edit `.env.local`:

```bash
export PRIVATE_INVESTMENT_ENABLED="true"
```

Reload the configuration and restart the bot:

```bash
source .env.local
python pawnstrading_bot.py
```

Test an amount below `$50` to confirm it is rejected, then test a valid amount and complete the risk, duration, consent, and payment-reference stages.

## 15. Inspect the local MongoDB records

Open a second Terminal window and run:

```bash
mongosh
```

Select the database:

```javascript
use pawns
```

List the collections:

```javascript
show collections
```

Inspect users:

```javascript
db.users.find().pretty();
```

Inspect registrations:

```javascript
db.submissions.find().sort({ created_at: -1 }).pretty();
```

Inspect audit entries:

```javascript
db.audit_log.find().sort({ created_at: -1 }).pretty();
```

Inspect referral commission events:

```javascript
db.commission_events.find().sort({ created_at: -1 }).pretty();
```

Exit MongoDB Shell with:

```javascript
exit;
```

## 16. Administrator commands

Only Telegram IDs listed in `ADMIN_CHAT_IDS` can use the administrator operations.

### View database totals

```text
/stats
```

### Update a registration or information link

```text
/setlink bingx https://example.com/bingx
/setlink broker https://example.com/broker
/setlink prop https://example.com/prop-firm
/setlink synthetic https://example.com/synthetic
/setlink support https://t.me/example_support
/setlink terms https://example.com/terms
/setlink investment_terms https://example.com/investment-terms
```

These values are stored in MongoDB and override the matching environment-variable URL.

### Record verified referral earnings

```text
/addcommission 123456789 100 USD Example commission event
```

With a 10% referral rate, an eligible affiliate commission of `USD 100` records `USD 10` as the referrer's verified share.

The first number is the referrer's Telegram ID, not the customer's investment amount.

## 17. Referral testing

1. Open the bot using one Telegram account.
2. Select **Referral Program**.
3. Copy the generated referral link.
4. Open the link using a different Telegram account.
5. Press **Start**.
6. Return to the first account and reopen **Referral Program**.
7. Confirm the attributed referral count increased.

The system rejects self-referrals and does not replace an existing attribution when the same user later opens another referral link.

## 18. Optional localhost webhook testing

Polling is recommended for ordinary development. Webhook mode is useful when testing production-like HTTPS delivery.

Telegram cannot send webhook requests directly to `localhost`. A public HTTPS tunnel is required. The example below uses ngrok.

### Install ngrok

```bash
brew install ngrok/ngrok/ngrok
```

Create an ngrok account, obtain the authentication token, and configure it:

```bash
ngrok config add-authtoken YOUR_NGROK_AUTH_TOKEN
```

### Start the tunnel

In a separate Terminal window:

```bash
ngrok http 8080
```

Copy the HTTPS forwarding URL, for example:

```text
https://example-random-name.ngrok-free.app
```

### Configure webhook mode

In the bot Terminal, load the normal configuration and add the webhook values:

```bash
cd ~/Projects/pawns-bot
source .venv/bin/activate
source .env.local

export RUN_MODE="webhook"
export WEBHOOK_BASE_URL="https://example-random-name.ngrok-free.app"
export WEBHOOK_PATH="telegram"
export WEBHOOK_SECRET="$(openssl rand -hex 32)"
export PORT="8080"
```

Start the webhook server:

```bash
python pawnstrading_bot.py
```

The local listener will use:

```text
http://127.0.0.1:8080/telegram
```

Telegram will use the public HTTPS address:

```text
https://example-random-name.ngrok-free.app/telegram
```

The application registers the complete public webhook URL automatically.

Free tunnel URLs may change when ngrok restarts. If the URL changes, update `WEBHOOK_BASE_URL` and restart the bot.

Do not run polling mode and webhook mode for the same bot token at the same time.

## 19. Return from webhook mode to polling

Stop the webhook server with `Control + C`, then run:

```bash
source .env.local
export RUN_MODE="polling"
python pawnstrading_bot.py
```

The Telegram library performs the polling startup process when the application starts.

If Telegram reports a webhook conflict, delete the existing webhook and retry:

```bash
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook?drop_pending_updates=false" | python3 -m json.tool
```

## 20. Environment-variable reference

### Required

| Variable         | Purpose                                                         |
| ---------------- | --------------------------------------------------------------- |
| `BOT_TOKEN`      | Telegram token issued by BotFather                              |
| `MONGODB_URI`    | Local or Atlas MongoDB connection string                        |
| `ADMIN_CHAT_IDS` | Comma-separated Telegram IDs authorised to review registrations |

### Database and runtime

| Variable           | Default    | Purpose                                        |
| ------------------ | ---------- | ---------------------------------------------- |
| `MONGODB_DATABASE` | `pawns`    | MongoDB database name                          |
| `RUN_MODE`         | `polling`  | `polling` or `webhook`                         |
| `LOG_LEVEL`        | `INFO`     | Application log level                          |
| `PORT`             | `8080`     | Local webhook listener port                    |
| `WEBHOOK_BASE_URL` | None       | Public HTTPS base URL                          |
| `WEBHOOK_PATH`     | `telegram` | Secret-ish URL path used by the listener       |
| `WEBHOOK_SECRET`   | None       | Telegram webhook secret-token validation value |

### Business configuration

| Variable               | Purpose                                                        |
| ---------------------- | -------------------------------------------------------------- |
| `ABOUT_TEXT`           | Company description displayed by the bot                       |
| `PAYMENT_INSTRUCTIONS` | Approved payment instructions shown before evidence submission |
| `SUPPORT_URL`          | Support page or Telegram support URL                           |
| `TERMS_URL`            | General terms and risk-disclosure URL                          |
| `INVESTMENT_TERMS_URL` | Investment agreement URL                                       |
| `BINGX_URL`            | BingX registration URL                                         |
| `BROKER_URL`           | Live broker registration URL                                   |
| `PROP_FIRM_URL`        | Prop-firm registration URL                                     |
| `SYNTHETIC_URL`        | Synthetic provider registration URL                            |

### Investment and referral configuration

| Variable                      | Default             | Purpose                                            |
| ----------------------------- | ------------------- | -------------------------------------------------- |
| `MINIMUM_INVESTMENT`          | `5`                 | Minimum accepted private-investment amount in USD  |
| `PRIVATE_INVESTMENT_ENABLED`  | `false`             | Explicitly enables private-investment submissions  |
| `RETURN_BASIS_TEXT`           | Built-in disclosure | Defines the legal meaning of the displayed figures |
| `REFERRAL_SECRET`             | Derived fallback    | Stable secret used to produce referral IDs         |
| `REFERRAL_COMMISSION_PERCENT` | `10`                | Share of eligible affiliate commission             |

## 21. Common errors and fixes

### `BOT_TOKEN is required`

The environment file was not loaded:

```bash
source .env.local
python pawnstrading_bot.py
```

### `MONGODB_URI is required`

Add the local connection string to `.env.local`:

```bash
export MONGODB_URI="mongodb://127.0.0.1:27017"
```

### MongoDB connection refused

Check the service:

```bash
brew services list | grep mongodb
```

Start it if necessary:

```bash
brew services start mongodb-community@8.0
```

Then test it:

```bash
mongosh --eval 'db.runCommand({ ping: 1 })'
```

### Administrator receives no DM

Check that:

- The numeric ID in `ADMIN_CHAT_IDS` is correct.
- The administrator has opened the bot and pressed **Start**.
- The administrator has not blocked the bot.
- The process is still running.
- The Terminal log does not show a Telegram `Forbidden` error.

### Telegram conflict error

Only one active polling process or webhook should use a bot token. Stop any duplicate process and retry.

To look for running copies:

```bash
ps aux | grep '[b]itmastery_bot.py'
```

### A link says “not configured”

Set the relevant environment variable or use the administrator `/setlink` command.

### Private investment says registration is disabled

This is the safe default. Complete the legal and payment configuration, then set:

```bash
export PRIVATE_INVESTMENT_ENABLED="true"
```

Reload `.env.local` and restart the application.

### Port 8080 is already in use

Find the process:

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
```

Stop that process or change `PORT` and the ngrok command to another port.

## 22. Stop or restart the local services

Stop the bot with `Control + C`.

Restart the bot after configuration changes:

```bash
source .venv/bin/activate
source .env.local
python pawnstrading_bot.py
```

Stop the local MongoDB service when it is no longer needed:

```bash
brew services stop mongodb-community@8.0
```

Start it again later with:

```bash
brew services start mongodb-community@8.0
```

## 23. Production notes

Before production deployment:

- Use a managed MongoDB deployment or secure the self-hosted database with authentication and restricted network access.
- Store secrets in the hosting provider's secret manager, not in source code.
- Use a stable HTTPS domain for webhook mode.
- Limit production database access to authorised staff.
- Define data retention and deletion procedures.
- Back up registrations and audit records.
- Replace all example URLs and placeholder wording.
- Verify the exact deliverable included in every service fee.
- Confirm payment instructions independently before publishing them.
- Keep private investment disabled until final agreements, disclosures, and return definitions are approved.
- Test payment verification, rejection, and user-notification paths before accepting live registrations.

## 24. Useful official documentation

- [python-telegram-bot Application documentation](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html)
- [MongoDB Community Edition installation on macOS](https://www.mongodb.com/docs/manual/administration/install-community-macos/)
- [PyMongo documentation](https://pymongo.readthedocs.io/en/stable/)
- [ngrok localhost tunnel documentation](https://ngrok.com/docs/start)
