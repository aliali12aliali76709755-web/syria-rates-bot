import logging
import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, APIRouter, HTTPException
from starlette.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiohttp

import rates_service as rs
import bot as botmod
from config import CURRENCIES
from database import db, client

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("server")

scheduler = AsyncIOScheduler(timezone="UTC")

tz_syp = timezone(timedelta(hours=3))

async def scheduled_daily_broadcast():
    if botmod.application is not None:
        await botmod.broadcast_rates_to_channels(botmod.application.bot)

async def scheduled_snapshot_and_change_check():
    try:
        await rs.snapshot_all()
    except Exception as e:
        logger.warning("Snapshot failed: %s", e)
    if botmod.application is not None:
        try:
            await botmod.check_and_broadcast_rate_changes(botmod.application.bot)
        except Exception as e:
            logger.error("Failed to check and broadcast rate changes: %s", e)

async def keep_awake_pinger():
    """Periodically ping the public URL to keep Render instance awake 24/7"""
    ping_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL")
    if not ping_url:
        logger.info("No RENDER_EXTERNAL_URL or PUBLIC_URL configured, skipping keep_awake_pinger.")
        return
    
    ping_url = ping_url.rstrip("/") + "/healthz"
    await asyncio.sleep(60) # Wait 1 min after startup
    logger.info("Starting Keep-Alive pinger to %s", ping_url)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ping_url, timeout=15) as resp:
                    if resp.status == 200:
                        logger.info("Keep-Alive ping successful: 200 OK")
        except Exception as e:
            logger.debug("Keep-Alive ping notice: %s", e)
        await asyncio.sleep(300) # Ping every 5 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    # indexes
    try:
        await db.rate_snapshots.create_index([("code", 1), ("source", 1), ("ts", 1)])
        await db.alerts.create_index([("chat_id", 1), ("active", 1)])
        await db.users.create_index("chat_id", unique=True)
        logger.info("Database indexes ensured.")
    except Exception as e:
        logger.error("Error creating database indexes: %s", e)

    # start telegram bot (long polling) — don't let a Telegram outage block the API
    application = None
    try:
        application = botmod.build_application()
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        await botmod.set_bot_commands(application.bot)
        logger.info("Telegram bot polling started and commands set")
    except Exception as e:
        logger.error("Telegram bot failed to start (REST API will still run): %s", e)

    # first snapshot + scheduled jobs
    try:
        await scheduled_snapshot_and_change_check()
    except Exception as e:
        logger.warning("Initial snapshot and change check failed: %s", e)
    
    scheduler.add_job(scheduled_snapshot_and_change_check, "interval", minutes=15, id="snapshot",
                      max_instances=1, coalesce=True)
    scheduler.add_job(botmod.check_alerts, "interval", minutes=2, id="alerts",
                      max_instances=1, coalesce=True)
    scheduler.add_job(scheduled_daily_broadcast, "cron", hour=9, minute=0, timezone=tz_syp, id="daily_broadcast",
                      max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("Scheduler started")

    # Start Keep-Alive pinger task
    asyncio.create_task(keep_awake_pinger())

    yield

    scheduler.shutdown(wait=False)
    if application is not None:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    client.close()


app = FastAPI(title="Syrian Exchange Rate Bot", lifespan=lifespan)
api = APIRouter(prefix="/api")


@app.get("/")
@app.get("/healthz")
async def root_health():
    return {
        "status": "online",
        "service": "Syrian Exchange Rate Bot",
        "bot": "@SyriaRates4Bot",
        "time": datetime.now(timezone.utc).isoformat()
    }


@api.get("/")
async def root():
    return {"message": "Syrian Exchange Rate Bot is running", "bot": "@SyriaRates4Bot"}


@api.get("/bot/status")
async def bot_status():
    users = await db.users.count_documents({})
    active_alerts = await db.alerts.count_documents({"active": True})
    snapshots = await db.rate_snapshots.count_documents({})
    me = None
    try:
        if botmod.application:
            u = await botmod.application.bot.get_me()
            me = {"id": u.id, "username": u.username, "first_name": u.first_name}
    except Exception:
        pass
    return {
        "online": botmod.application is not None,
        "bot": me,
        "users": users,
        "active_alerts": active_alerts,
        "snapshots": snapshots,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@api.get("/rates")
async def get_rates():
    return await rs.get_rates()


@api.get("/rates/{code}")
async def get_rate(code: str):
    data = await rs.get_rates()
    code = code.upper()
    if code in data["currencies"]:
        return data["currencies"][code]
    if code in data["gold"]:
        return data["gold"][code]
    raise HTTPException(status_code=404, detail="Currency not found")


@api.get("/history/{code}")
async def history(code: str, days: int = 7, source: str | None = None):
    code = code.upper()
    pts, used = await rs.get_history(code, days=days, source=source)
    return {
        "code": code, "days": days, "source": used,
        "points": [{"ts": dt.isoformat(), "mid": v} for dt, v in pts],
    }


@api.get("/stats")
async def stats():
    users = await db.users.count_documents({})
    return {
        "users": users,
        "active_alerts": await db.alerts.count_documents({"active": True}),
        "triggered_alerts": await db.alerts.count_documents({"active": False, "triggered_at": {"$exists": True}}),
        "currencies": list(CURRENCIES.keys()),
    }


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
