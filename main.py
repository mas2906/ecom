"""
Kullanım:
  python main.py --platform trendyol --category "laptop" --pages 5
  python main.py --platform amazon --category "kulaklık" --pages 4
  python main.py --all --category "laptop" --pages 3   # tüm platformlar sırayla
  python main.py --jobs                                 # jobs.json'daki tüm işleri çalıştır
  python main.py --jobs my_jobs.json                    # özel dosya

Ortam değişkenleri (.env dosyasına koy):
  DB_URL                 postgresql://user:pass@localhost:5432/ecom_scraper
  TELEGRAM_TOKEN         BotFather'dan aldığın token
  TELEGRAM_CHAT_ID       Bildirimlerin gideceği chat ID
  TELEGRAM_MIN_DROP_PCT  Minimum düşüş yüzdesi (varsayılan: 0)
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from config import PLATFORMS
from anti_ban import ScraperSession
from scrapers import AmazonScraper, TrendyolScraper
from storage import Storage

load_dotenv("ignored/.env")

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%H:%M:%S]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger(__name__)

SCRAPER_MAP = {
    "trendyol": TrendyolScraper,
    "amazon": AmazonScraper,
}

PLATFORM_COLORS = {
    "trendyol": "bold orange3",
    "amazon": "bold cyan",
}


def _url_platform(category: str) -> Optional[str]:
    """Kategori bir URL ise hangi platforma ait olduğunu döndürür, keyword ise None."""
    if not (category.startswith("http://") or category.startswith("https://")):
        return None
    if "trendyol.com" in category:
        return "trendyol"
    if "amazon.com" in category:
        return "amazon"
    return None


async def run_platform(
    platform: str,
    category: str,
    max_pages: int,
    storage: Storage,
) -> int:
    url_plt = _url_platform(category)
    if url_plt and platform != url_plt:
        logger.info("URL sadece '%s' için — %s atlanıyor", url_plt, platform)
        return 0

    config = PLATFORMS[platform]
    color = PLATFORM_COLORS.get(platform, "bold white")

    console.print(
        Panel(
            f"[{color}]{platform.upper()}[/{color}]  ·  kategori: [cyan]{category}[/cyan]  ·  max sayfa: {max_pages}",
            expand=False,
        )
    )

    async with ScraperSession(config) as session:
        scraper = SCRAPER_MAP[platform](session)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(f"[{color}]{platform}[/{color}] scraping…", total=None)
            count = 0

            async for product in scraper.scrape_category(category, max_pages):
                await storage.save(product)
                count += 1
                price_str = f"{product.price:,.2f} TRY" if product.price else "—"
                progress.update(
                    task,
                    description=(
                        f"[{color}]{platform}[/{color}]  "
                        f"[{count}] {product.title[:48]}…  {price_str}"
                    ),
                )

    console.print(f"  ✓ [green]{count}[/green] ürün kaydedildi\n")
    return count


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Trendyol / Amazon TR — Multi-Platform Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--platform", choices=list(SCRAPER_MAP.keys()), help="Hedef platform")
    p.add_argument("--category", help="Arama terimi veya kategori adı")
    p.add_argument("--pages", type=int, default=5, help="Maksimum sayfa sayısı (varsayılan: 5)")
    p.add_argument("--output", default="./ignored/output", help="Çıktı klasörü (varsayılan: ./ignored/output)")
    p.add_argument("--all", action="store_true", help="Tüm platformlarda sırayla çalıştır")
    p.add_argument(
        "--jobs",
        nargs="?",
        const="jobs.json",
        metavar="DOSYA",
        help="jobs.json'daki tüm işleri çalıştır (varsayılan: jobs.json)",
    )
    return p.parse_args()


def _load_jobs(path: str) -> list[dict]:
    if not os.path.exists(path):
        console.print(f"[red]jobs dosyası bulunamadı: {path}[/red]")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        jobs = json.load(f)
    valid = []
    for job in jobs:
        if not job.get("category"):
            console.print(f"[yellow]Uyarı: 'category' eksik, atlanıyor: {job}[/yellow]")
            continue
        platforms = job.get("platforms") or list(SCRAPER_MAP.keys())
        unknown = [p for p in platforms if p not in SCRAPER_MAP]
        if unknown:
            console.print(f"[yellow]Bilinmeyen platform(lar) atlanıyor: {unknown}[/yellow]")
            platforms = [p for p in platforms if p in SCRAPER_MAP]
        if platforms:
            valid.append({
                "category": job["category"],
                "platforms": platforms,
                "pages": int(job.get("pages", 5)),
            })
    return valid


async def _setup_db_and_notifier():
    db_pool: Optional[object] = None
    db_url = os.getenv("DB_URL")
    if db_url:
        try:
            from db import create_pool, setup_schema
            db_pool = await create_pool(db_url)
            await setup_schema(db_pool)
        except Exception as exc:
            logger.warning("PostgreSQL bağlantısı kurulamadı (%s) — sadece dosyaya yazılacak", exc)
    else:
        logger.info("DB_URL bulunamadı — PostgreSQL atlanıyor")

    notifier = None
    tg_token = os.getenv("TELEGRAM_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        from notifier import Notifier
        min_drop = float(os.getenv("TELEGRAM_MIN_DROP_PCT", "0"))
        notifier = Notifier(token=tg_token, chat_id=tg_chat, min_drop_pct=min_drop)
        logger.info("Telegram bildirimleri aktif (min düşüş: %%%.1f)", min_drop)
    else:
        logger.info("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID bulunamadı — bildirim kapalı")

    return db_pool, notifier, db_url


async def main() -> None:
    args = parse_args()

    if not args.jobs and not args.platform and not args.all:
        console.print("[red]--platform, --all veya --jobs belirtmelisiniz[/red]")
        sys.exit(1)

    if not args.jobs and not args.category:
        console.print("[red]--category gerekli (--jobs kullanmıyorsanız)[/red]")
        sys.exit(1)

    db_pool, notifier, db_url = await _setup_db_and_notifier()
    storage = Storage(args.output, db_pool=db_pool, notifier=notifier)
    total = 0

    if args.jobs:
        jobs = _load_jobs(args.jobs)
        console.print(f"[bold]jobs.json:[/bold] {len(jobs)} iş yüklendu\n")
        for job in jobs:
            for platform in job["platforms"]:
                total += await run_platform(platform, job["category"], job["pages"], storage)
    else:
        platforms = list(SCRAPER_MAP.keys()) if args.all else [args.platform]
        for platform in platforms:
            total += await run_platform(platform, args.category, args.pages, storage)

    await storage.flush()

    if db_pool:
        await db_pool.close()

    db_line = f"PostgreSQL  → {db_url.split('@')[-1] if db_url else '—'}\n" if db_url else ""
    console.print(
        Panel(
            f"[bold green]Tamamlandı![/bold green]  Toplam [cyan]{total}[/cyan] ürün\n"
            f"JSON  → {storage.json_path}\n"
            f"CSV   → {storage.csv_path}\n"
            f"{db_line}",
            title="Çıktı",
            expand=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
