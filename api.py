"""
FastAPI web arayüzü — http://localhost:8000
"""
import asyncio
import json
import logging
import os
import signal
import uuid
from pathlib import Path

import uvicorn
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from pipeline.scheduler import AdaptiveScheduler, next_interval_hours_for_category

load_dotenv("ignored/.env")

_jobs: dict[str, asyncio.Queue] = {}
_tasks: dict[str, asyncio.Task] = {}
_SCHEDULE_FILE = Path("schedule.json")
_POOL_FILE = Path("category_pool.json")
_JOBS_FILE = Path("jobs.json")
scheduler = AsyncIOScheduler(timezone="Europe/Istanbul")

_adaptive_scheduler: "AdaptiveScheduler | None" = None
_adaptive_pool = None

# Manuel "Tara" başlatma açık.
MANUAL_SCAN_ENABLED = True

# ── Canlı yayın — otomatik/adaptif taramaların log'unu arayüze akıtır ──────────
# (manuel tarama kapalıyken arayüzün hâlâ ne olduğunu görebilmesi için)
_live_subscribers: list[asyncio.Queue] = []


class _LiveBroadcastHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            msg = {"type": "log", "level": record.levelname, "msg": self.format(record)}
            for q in _live_subscribers:
                q.put_nowait(msg)
        except Exception:
            pass


logging.getLogger().addHandler(_LiveBroadcastHandler())
logging.getLogger().setLevel(logging.INFO)


# ── Category pool persistence ──────────────────────────────────────────────────

def _load_pool() -> list[str]:
    if _POOL_FILE.exists():
        try:
            return json.loads(_POOL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_pool(items: list[str]) -> None:
    _POOL_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    times = _load_schedule()
    if times:
        _apply_schedule(times)
    await _start_adaptive_scheduling()
    yield
    scheduler.shutdown(wait=False)
    if _adaptive_scheduler:
        _adaptive_scheduler.shutdown()
    if _adaptive_pool:
        await _adaptive_pool.close()


app = FastAPI(title="Ecom Scraper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="ui/static"), name="static")


# ── Schedule persistence ───────────────────────────────────────────────────────

def _load_schedule() -> list[str]:
    """['09:00', '21:00'] formatında saat listesi döndürür."""
    if _SCHEDULE_FILE.exists():
        try:
            return json.loads(_SCHEDULE_FILE.read_text())
        except Exception:
            pass
    return []


def _save_schedule(times: list[str]) -> None:
    _SCHEDULE_FILE.write_text(json.dumps(times))


def _apply_schedule(times: list[str]) -> None:
    """Mevcut schedule job'larını temizle ve yenilerini ekle."""
    for job in scheduler.get_jobs():
        if job.id.startswith("auto_scan_"):
            job.remove()
    for t in times:
        try:
            hour, minute = t.split(":")
            scheduler.add_job(
                _auto_scan,
                CronTrigger(hour=int(hour), minute=int(minute)),
                id=f"auto_scan_{t.replace(':','_')}",
                replace_existing=True,
                # Varsayılan 1sn çok dar — sunucu tam o an meşgul/reload
                # oluyorsa tetikleme sessizce ertesi güne kaçıyordu.
                misfire_grace_time=3600,
            )
            logging.getLogger(__name__).info("Zamanlayıcı eklendi: %s", t)
        except Exception as e:
            logging.getLogger(__name__).warning("Zamanlayıcı hatası %s: %s", t, e)


async def _auto_scan() -> None:
    """Otomatik zamanlı tarama — jobs.json'daki tüm işleri çalıştırır."""
    jobs_file = Path("jobs.json")
    if not jobs_file.exists():
        return
    jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
    logger = logging.getLogger(__name__)
    logger.info("⏰ Otomatik zamanlı tarama başladı")

    from main import SCRAPER_MAP, run_platform
    from storage import Storage

    db_pool = None
    db_url = os.getenv("DB_URL")
    if db_url:
        try:
            from db import create_pool, setup_schema
            db_pool = await create_pool(db_url)
            await setup_schema(db_pool)
        except Exception as e:
            logger.warning("PostgreSQL: %s", e)

    notifier = None
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        from notifier import Notifier
        notifier = Notifier(token=token, chat_id=chat,
                            min_drop_pct=float(os.getenv("TELEGRAM_MIN_DROP_PCT", "0")))

    try:
        storage = Storage("./ignored/output", db_pool=db_pool, notifier=notifier)
        for job in jobs:
            category = job.get("category", "")
            pages = job.get("pages", 3)
            for platform in job.get("platforms", []):
                if platform not in SCRAPER_MAP:
                    continue
                try:
                    count = await run_platform(platform, category, pages, storage)
                    logger.info("  ✓ %s / %s → %d ürün", platform, category, count)
                except Exception as e:
                    # Tek bir iş başarısız olsa bile kalan işler devam etsin.
                    logger.error("  ✗ %s / %s başarısız: %s", platform, category, e)
        await storage.flush()

        # Barkod zenginleştirme
        if db_pool:
            from barcode_enricher import enrich_barcodes
            await enrich_barcodes(db_pool, platform=None, limit=60, concurrency=2)

        # Çapraz eşleşme kontrolü
        if db_pool and notifier:
            await _notify_cross_matches(db_pool, notifier)
    except Exception as e:
        logger.error("Otomatik tarama hatası: %s", e)
    finally:
        if db_pool:
            await db_pool.close()


async def _start_adaptive_scheduling() -> None:
    """
    jobs.json'daki her (platform, kategori) çifti için fiyat oynaklığına göre
    adaptif tarama aralığı belirler — schedule.json'daki sabit saatlerin
    yerine değil, onlara EK olarak çalışır. DB_URL yoksa devre dışı kalır
    (medyan hesaplamak için price_history gerekiyor).
    """
    global _adaptive_scheduler, _adaptive_pool
    logger = logging.getLogger(__name__)

    db_url = os.getenv("DB_URL")
    if not db_url:
        logger.info("DB_URL yok — adaptif zamanlama atlandı")
        return
    if not _JOBS_FILE.exists():
        return

    from db import create_pool, setup_schema
    _adaptive_pool = await create_pool(db_url)
    await setup_schema(_adaptive_pool)

    async def interval_fn(platform: str, category: str) -> float:
        return await next_interval_hours_for_category(_adaptive_pool, platform, category)

    _adaptive_scheduler = AdaptiveScheduler(interval_fn=interval_fn, callback=_run_adaptive_job)
    _adaptive_scheduler.start()

    jobs = json.loads(_JOBS_FILE.read_text(encoding="utf-8"))
    for job in jobs:
        category = job.get("category", "")
        if not category:
            continue
        for platform in job.get("platforms", []):
            await _adaptive_scheduler.schedule(platform, category)
    logger.info("Adaptif zamanlama başladı (%d iş)", len(jobs))


async def _run_adaptive_job(platform: str, category: str) -> None:
    """Tek bir (platform, kategori) işini çalıştırır — jobs.json'daki sayfa sayısını kullanır."""
    logger = logging.getLogger(__name__)
    if not _JOBS_FILE.exists():
        return
    jobs = json.loads(_JOBS_FILE.read_text(encoding="utf-8"))
    job = next(
        (j for j in jobs if j.get("category") == category and platform in j.get("platforms", [])),
        None,
    )
    if not job:
        return

    from main import SCRAPER_MAP, run_platform
    if platform not in SCRAPER_MAP:
        return
    from storage import Storage

    notifier = None
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        from notifier import Notifier
        notifier = Notifier(
            token=token, chat_id=chat,
            min_drop_pct=float(os.getenv("TELEGRAM_MIN_DROP_PCT", "0")),
        )

    logger.info("🔄 Adaptif tarama: %s / %s", platform, category)
    storage = Storage("./ignored/output", db_pool=_adaptive_pool, notifier=notifier)
    try:
        count = await run_platform(platform, category, job.get("pages", 3), storage)
        logger.info("  ✓ adaptif %s / %s → %d ürün", platform, category, count)
    except Exception as e:
        # Bu iş başarısız olsa bile scheduler kendini yeniden zamanlamaya devam etsin.
        logger.error("  ✗ adaptif %s / %s başarısız: %s", platform, category, e)
    finally:
        await storage.flush()


async def _notify_cross_matches(pool, notifier) -> None:
    from db import get_cross_matches
    matches = await get_cross_matches(pool)
    logger = logging.getLogger(__name__)
    logger.info("Çapraz eşleşme: %d ürün", len(matches))
    for match in matches:
        await notifier.cross_match(match)


# ── FastAPI startup/shutdown ───────────────────────────────────────────────────

# ── Models ─────────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    platforms: list[str]
    categories: list[str]
    pages: int = 3


class ScheduleRequest(BaseModel):
    times: list[str]   # ["09:00", "21:00"]


class BadgeScanRequest(BaseModel):
    categories: list[str] = []   # boş → varsayılan kategoriler
    pages: int = 2


class PoolRequest(BaseModel):
    items: list[str]


# ── Queue log handler ──────────────────────────────────────────────────────────

class _QueueHandler(logging.Handler):
    def __init__(self, q: asyncio.Queue):
        super().__init__()
        self._q = q
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            self._q.put_nowait({
                "type": "log",
                "level": record.levelname,
                "msg": self.format(record),
            })
        except Exception:
            pass


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("ui/index.html", encoding="utf-8") as f:
        return f.read()


@app.post("/scrape")
async def start_scrape(req: ScrapeRequest):
    if not MANUAL_SCAN_ENABLED:
        raise HTTPException(423, "Manuel tarama devre dışı — sadece otomatik zamanlanmış taramalar çalışır")

    from main import SCRAPER_MAP
    bad = [p for p in req.platforms if p not in SCRAPER_MAP]
    if bad:
        raise HTTPException(400, f"Geçersiz platform: {bad}")
    if not req.categories:
        raise HTTPException(400, "En az bir kategori girin")

    job_id = str(uuid.uuid4())[:8]
    q: asyncio.Queue = asyncio.Queue()
    _jobs[job_id] = q
    task = asyncio.create_task(_run(job_id, req, q))
    _tasks[job_id] = task
    return {"job_id": job_id}


async def _run(job_id: str, req: ScrapeRequest, q: asyncio.Queue):
    from main import SCRAPER_MAP, run_platform
    from storage import Storage

    db_pool = None
    db_url = os.getenv("DB_URL")
    if db_url:
        try:
            from db import create_pool, setup_schema
            db_pool = await create_pool(db_url)
            await setup_schema(db_pool)
        except Exception as e:
            q.put_nowait({"type": "log", "level": "WARNING", "msg": f"PostgreSQL: {e}"})

    notifier = None
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        from notifier import Notifier
        notifier = Notifier(
            token=token, chat_id=chat,
            min_drop_pct=float(os.getenv("TELEGRAM_MIN_DROP_PCT", "0")),
        )

    handler = _QueueHandler(q)
    root = logging.getLogger()
    root.addHandler(handler)

    total = 0
    cancelled = False
    try:
        storage = Storage("./ignored/output", db_pool=db_pool, notifier=notifier)

        async def _scrape_one(platform: str, category: str) -> int:
            q.put_nowait({"type": "log", "level": "INFO",
                          "msg": f"▶ {platform.upper()} → \"{category}\""})
            count = await run_platform(platform, category, req.pages, storage)
            q.put_nowait({"type": "platform_done",
                          "platform": platform, "category": category, "count": count})
            return count

        # Her kategori için tüm platformları aynı anda başlat
        for category in req.categories:
            results = await asyncio.gather(
                *[_scrape_one(p, category) for p in req.platforms],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, int):
                    total += r
                elif isinstance(r, Exception):
                    q.put_nowait({"type": "log", "level": "ERROR", "msg": str(r)})
        await storage.flush()

        # Barkod zenginleştirme — barkodu olmayan ürünlerin detay sayfaları ziyaret edilir
        if db_pool:
            q.put_nowait({"type": "log", "level": "INFO",
                          "msg": "🏷️ Barkod zenginleştirme başlıyor…"})
            try:
                from barcode_enricher import enrich_barcodes
                n = await enrich_barcodes(db_pool, platform=None, limit=60, concurrency=2)
                q.put_nowait({"type": "log", "level": "INFO",
                              "msg": f"🏷️ {n} ürüne barkod eklendi"})
            except Exception as e:
                q.put_nowait({"type": "log", "level": "WARNING",
                              "msg": f"Barkod zenginleştirme hatası: {e}"})

        # Çapraz eşleşme kontrolü ve bildirim
        if db_pool and notifier:
            matches = await _get_and_notify_matches(db_pool, notifier)
            if matches:
                q.put_nowait({"type": "log", "level": "INFO",
                              "msg": f"🔗 {len(matches)} çapraz barkod eşleşmesi bulundu"})
    except asyncio.CancelledError:
        cancelled = True
        q.put_nowait({"type": "log", "level": "WARNING", "msg": "⛔ Tarama kullanıcı tarafından durduruldu"})
    except Exception as e:
        q.put_nowait({"type": "log", "level": "ERROR", "msg": str(e)})
    finally:
        root.removeHandler(handler)
        if db_pool:
            await db_pool.close()

    if cancelled:
        q.put_nowait({"type": "cancelled", "total": total})
    else:
        q.put_nowait({"type": "done", "total": total})


async def _get_and_notify_matches(pool, notifier) -> list:
    try:
        from db import get_cross_matches
        matches = await get_cross_matches(pool)
        for match in matches:
            await notifier.cross_match(match)
        return matches
    except Exception:
        return []


@app.get("/stream/live")
async def stream_live():
    """
    Otomatik/adaptif taramaların canlı log akışı — manuel tarama kapalıyken
    bile arayüzün ne olup bittiğini gösterebilmesi için. job_id gerektirmez,
    birden fazla sekme aynı anda dinleyebilir.
    """
    q: asyncio.Queue = asyncio.Queue()
    _live_subscribers.append(q)

    async def gen():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _live_subscribers.remove(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/stream/{job_id}")
async def stream_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "İş bulunamadı")
    q = _jobs[job_id]

    async def gen():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") in ("done", "cancelled"):
                    break
            except asyncio.TimeoutError:
                yield ": ping\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/cancel/{job_id}")
async def cancel_job(job_id: str):
    task = _tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        return {"ok": True}
    return {"ok": False}


@app.get("/recent")
async def recent():
    db_url = os.getenv("DB_URL")
    if not db_url:
        return []
    try:
        from db import create_pool, get_recent
        pool = await create_pool(db_url)
        rows = await get_recent(pool)
        await pool.close()
        return rows
    except Exception:
        return []


@app.get("/matches")
async def cross_matches():
    db_url = os.getenv("DB_URL")
    if not db_url:
        return []
    try:
        from db import create_pool, get_cross_matches
        pool = await create_pool(db_url)
        rows = await get_cross_matches(pool)
        await pool.close()
        return rows
    except Exception:
        return []


@app.post("/scan-badges")
async def start_badge_scan(req: BadgeScanRequest):
    job_id = "b" + str(uuid.uuid4())[:7]
    q: asyncio.Queue = asyncio.Queue()
    _jobs[job_id] = q
    task = asyncio.create_task(_run_badge_scan(job_id, req, q))
    _tasks[job_id] = task
    return {"job_id": job_id}


async def _run_badge_scan(job_id: str, req: BadgeScanRequest, q: asyncio.Queue):
    from badge_scanner import scan_badges, DEFAULT_CATEGORIES
    from db import create_pool, setup_schema, save_product

    db_pool = None
    db_url = os.getenv("DB_URL")
    if db_url:
        try:
            db_pool = await create_pool(db_url)
            await setup_schema(db_pool)
        except Exception as e:
            q.put_nowait({"type": "log", "level": "WARNING", "msg": f"PostgreSQL: {e}"})

    notifier = None
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        from notifier import Notifier
        notifier = Notifier(
            token=token, chat_id=chat,
            min_drop_pct=float(os.getenv("TELEGRAM_MIN_DROP_PCT", "0")),
        )

    handler = _QueueHandler(q)
    root = logging.getLogger()
    root.addHandler(handler)

    total = 0
    cancelled = False
    try:
        cats = req.categories or DEFAULT_CATEGORIES
        q.put_nowait({"type": "log", "level": "SYSTEM",
                      "msg": f"🏷️ Rozet taraması başlıyor — {len(cats)} kategori × {req.pages} sayfa"})

        async def on_product(p):
            nonlocal total
            total += 1
            q.put_nowait({"type": "badge_product", "title": p.title[:60],
                          "badge": p.price_badge, "price": p.price, "url": p.url, "count": total})
            if db_pool and p.price is not None:
                try:
                    await save_product(db_pool, p)
                except Exception:
                    pass
            if notifier and p.price_badge:
                try:
                    await notifier.price_badge_alert(p)
                except Exception:
                    pass

        async def on_progress(msg):
            q.put_nowait({"type": "log", "level": "INFO", "msg": msg})

        await scan_badges(
            categories=cats,
            pages=req.pages,
            on_product=on_product,
            on_progress=on_progress,
        )
    except asyncio.CancelledError:
        cancelled = True
        q.put_nowait({"type": "log", "level": "WARNING", "msg": "⛔ Rozet taraması durduruldu"})
    except Exception as e:
        q.put_nowait({"type": "log", "level": "ERROR", "msg": str(e)})
    finally:
        root.removeHandler(handler)
        if db_pool:
            await db_pool.close()

    if cancelled:
        q.put_nowait({"type": "cancelled", "total": total})
    else:
        q.put_nowait({"type": "done", "total": total})


@app.get("/stream-badges/{job_id}")
async def stream_badge_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "İş bulunamadı")
    q = _jobs[job_id]

    async def gen():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") in ("done", "cancelled"):
                    break
            except asyncio.TimeoutError:
                yield ": ping\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/badge-products")
async def badge_products():
    db_url = os.getenv("DB_URL")
    if not db_url:
        return []
    try:
        from db import create_pool, get_badge_products
        pool = await create_pool(db_url)
        result = await get_badge_products(pool)
        await pool.close()
        return result
    except Exception:
        return []


@app.get("/pool")
async def get_pool():
    return {"items": _load_pool()}


@app.post("/pool")
async def add_to_pool(req: PoolRequest):
    pool = _load_pool()
    added = []
    for raw in req.items:
        raw = raw.strip()
        if not raw:
            continue
        # URL'leri olduğu gibi sakla, metin kategorileri küçük harfe çevir
        item = raw if raw.startswith("http://") or raw.startswith("https://") else raw.lower()
        if item not in pool:
            pool.append(item)
            added.append(item)
    _save_pool(pool)
    return {"items": pool, "added": added}


@app.delete("/pool/{item:path}")
async def remove_from_pool(item: str):
    pool = [p for p in _load_pool() if p != item]
    _save_pool(pool)
    return {"items": pool}


@app.get("/schedule")
async def get_schedule():
    return {"times": _load_schedule()}


@app.post("/schedule")
async def set_schedule(req: ScheduleRequest):
    # "HH:MM" formatını doğrula
    for t in req.times:
        parts = t.split(":")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            raise HTTPException(400, f"Geçersiz saat formatı: {t} (HH:MM olmalı)")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise HTTPException(400, f"Geçersiz saat: {t}")
    _save_schedule(req.times)
    _apply_schedule(req.times)
    return {"ok": True, "times": req.times}


@app.post("/shutdown")
async def shutdown():
    """Sunucuyu kapatır — Mac .app içinden çağrılır."""
    asyncio.get_event_loop().call_later(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"ok": True}


if __name__ == "__main__":
    import os as _os
    dev = _os.getenv("ENV", "dev") != "prod"
    uvicorn.run(
        "api:app", host="127.0.0.1", port=8000, reload=dev, reload_dirs=["."],
        # /stream/live gibi açık SSE bağlantıları reload'ı sonsuza kadar
        # bekletmesin diye kapanma süresini sınırlıyoruz.
        timeout_graceful_shutdown=3,
    )
