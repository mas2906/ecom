#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# PostgreSQL
if ! /opt/homebrew/Cellar/postgresql@15/15.14/bin/pg_isready -q 2>/dev/null; then
  brew services start postgresql@15
  sleep 2
fi

source .venv/bin/activate

echo ""
echo "  ⚡ Ecom Scraper UI başlatılıyor..."
echo "  → http://localhost:8000"
echo ""

python api.py
