#!/usr/bin/env bash
# Tüm jobs.json'daki işleri çalıştırır.
# Kullanım: bash run.sh
#           bash run.sh --platform trendyol --category "laptop" --pages 2

set -e
cd "$(dirname "$0")"

# PostgreSQL ayakta değilse başlat
if ! /opt/homebrew/Cellar/postgresql@15/15.14/bin/pg_isready -q 2>/dev/null; then
  brew services start postgresql@15
  sleep 2
fi

source .venv/bin/activate

if [ $# -eq 0 ]; then
  python main.py --jobs
else
  python main.py "$@"
fi
