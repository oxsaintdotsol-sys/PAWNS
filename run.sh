#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -d ".venv" ]; then
  echo "Virtual environment .venv not found. Creating it..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

if [ ! -f ".env.local" ]; then
  echo "Error: .env.local file not found. Please copy .env.example to .env.local and configure your credentials."
  exit 1
fi

source .venv/bin/activate
source .env.local

echo "Checking configuration..."
if [[ "$BOT_TOKEN" == *"PASTE_"* || -z "$BOT_TOKEN" ]]; then
  echo "Error: BOT_TOKEN is not configured in .env.local"
  exit 1
fi

if [[ "$ADMIN_CHAT_IDS" == *"PASTE_"* || -z "$ADMIN_CHAT_IDS" ]]; then
  echo "Error: ADMIN_CHAT_IDS is not configured in .env.local"
  exit 1
fi

echo "Starting PAWNS Bot in ${RUN_MODE:-polling} mode..."
exec python pawnstrading_bot.py
