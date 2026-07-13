#!/usr/bin/env bash
set -e

echo "==> Sanal ortam oluşturuluyor..."
python3 -m venv .venv
source .venv/bin/activate

echo "==> Bağımlılıklar kuruluyor..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "==> Playwright Chromium indiriliyor..."
playwright install chromium

echo ""
echo "==> ignored/.env dosyası oluşturuluyor (yoksa)..."
mkdir -p ignored
if [ ! -f ignored/.env ]; then
    cp .env.example ignored/.env
    echo "  → ignored/.env oluşturuldu. DB_URL ve Telegram bilgilerini düzenle."
fi

echo ""
echo "Kurulum tamamlandı!"
echo ""
echo "1) ignored/.env dosyasını düzenle:"
echo "   DB_URL=postgresql://postgres:sifre@localhost:5432/ecom_scraper"
echo "   TELEGRAM_TOKEN=..."
echo "   TELEGRAM_CHAT_ID=..."
echo ""
echo "2) Kullanım örnekleri:"
echo "   source .venv/bin/activate"
echo "   python main.py --platform trendyol --category 'laptop' --pages 5"
echo "   python main.py --platform hepsiburada --category 'akıllı telefon' --pages 3"
echo "   python main.py --platform amazon --category 'kulaklık' --pages 4"
echo "   python main.py --all --category 'laptop' --pages 3"
