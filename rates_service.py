"""Fetches, normalizes and caches Syrian-pound exchange rates from two free sources:
LiraScope (central bank + market) and LiraNews (market + gold)."""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

import httpx

from config import (
    LIRASCOPE_BASE, LIRANEWS_BASE, CURRENCIES, GOLD, CURRENCY_ORDER, GOLD_ORDER,
)
from database import db

logger = logging.getLogger("rates")

_CACHE = {"data": None, "ts": 0.0}
_CACHE_TTL = 60  # seconds


async def _fetch_lirascope():
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{LIRASCOPE_BASE}/rates/latest", params={"lang": "ar"})
        r.raise_for_status()
        return r.json()


async def _fetch_liranews():
    symbols = []
    for cfg in CURRENCIES.values():
        if cfg["liranews"]:
            symbols.append(cfg["liranews"])
    for cfg in GOLD.values():
        if cfg["liranews"]:
            symbols.append(cfg["liranews"])
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{LIRANEWS_BASE}/price/" + ",".join(symbols))
        r.raise_for_status()
        return r.json()


def _lirascope_index(payload):
    """Return {CODE: {'market': {...}, 'cbs': {...}}}"""
    out = {}
    for r in payload.get("marketRates", []):
        out.setdefault(r["currency"], {})["market"] = {
            "buy": r.get("buy"), "sell": r.get("sell"), "mid": r.get("mid"),
            "ts": r.get("timestampUtc"),
        }
    for r in payload.get("cbsRates", []):
        out.setdefault(r["currency"], {})["cbs"] = {
            "buy": r.get("buy"), "sell": r.get("sell"), "mid": r.get("mid"),
            "ts": r.get("timestampUtc"),
        }
    return out


async def get_rates(force=False):
    """Merged snapshot. Cached for 60s."""
    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    ls_payload, ln_payload = {}, {}
    try:
        ls_payload, ln_payload = await asyncio.gather(
            _fetch_lirascope(), _fetch_liranews()
        )
    except Exception as e:
        logger.warning("Partial fetch failure: %s", e)
        # try individually so one broken source doesn't kill everything
        try:
            ls_payload = await _fetch_lirascope()
        except Exception as e2:
            logger.warning("LiraScope failed: %s", e2)
        try:
            ln_payload = await _fetch_liranews()
        except Exception as e2:
            logger.warning("LiraNews failed: %s", e2)

    ls_idx = _lirascope_index(ls_payload) if ls_payload else {}

    currencies = {}
    for code in CURRENCY_ORDER:
        cfg = CURRENCIES[code]
        entry = {
            "code": code, "name_ar": cfg["name_ar"], "flag": cfg["flag"],
            "lirascope_market": None, "lirascope_cbs": None, "liranews": None,
        }
        ls = ls_idx.get(cfg["lirascope"]) if cfg["lirascope"] else None
        if ls:
            entry["lirascope_market"] = ls.get("market")
            entry["lirascope_cbs"] = ls.get("cbs")
        ln_sym = cfg["liranews"]
        if ln_sym and ln_payload and ln_sym in ln_payload:
            d = ln_payload[ln_sym]
            entry["liranews"] = {
                "buy": d.get("buy"), "sell": d.get("sell"), "mid": d.get("value"),
                "last_close": d.get("last_close"), "ts": d.get("price_updated_at"),
            }
        currencies[code] = entry

    gold = {}
    for code in GOLD_ORDER:
        cfg = GOLD[code]
        ln_sym = cfg["liranews"]
        item = {"code": code, "name_ar": cfg["name_ar"], "liranews": None}
        if ln_sym and ln_payload and ln_sym in ln_payload:
            d = ln_payload[ln_sym]
            item["liranews"] = {
                "buy": d.get("buy"), "sell": d.get("sell"), "mid": d.get("value"),
                "last_close": d.get("last_close"), "ts": d.get("price_updated_at"),
            }
        gold[code] = item

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currencies": currencies,
        "gold": gold,
        "sources": ["LiraScope", "LiraNews"],
    }
    _CACHE["data"] = data
    _CACHE["ts"] = now
    return data


def market_mid(entry):
    """Best available market mid price for a currency entry (LiraNews preferred)."""
    if entry.get("liranews") and entry["liranews"].get("mid") is not None:
        return entry["liranews"]["mid"]
    if entry.get("lirascope_market") and entry["lirascope_market"].get("mid") is not None:
        return entry["lirascope_market"]["mid"]
    return None


async def snapshot_all():
    """Fetch fresh rates and persist point-in-time snapshots for history/charts."""
    data = await get_rates(force=True)
    ts = data["generated_at"]
    docs = []
    for code, entry in data["currencies"].items():
        for src_key, src_name in (("lirascope_market", "lirascope_market"),
                                   ("lirascope_cbs", "lirascope_cbs"),
                                   ("liranews", "liranews")):
            v = entry.get(src_key)
            if v and v.get("mid") is not None:
                docs.append({
                    "code": code, "kind": "currency", "source": src_name,
                    "buy": v.get("buy"), "sell": v.get("sell"), "mid": v.get("mid"),
                    "ts": ts,
                })
    for code, item in data["gold"].items():
        v = item.get("liranews")
        if v and v.get("mid") is not None:
            docs.append({
                "code": code, "kind": "gold", "source": "liranews",
                "buy": v.get("buy"), "sell": v.get("sell"), "mid": v.get("mid"),
                "ts": ts,
            })
    if docs:
        await db.rate_snapshots.insert_many(docs)
    logger.info("Stored %d rate snapshots", len(docs))
    return len(docs)


async def get_history(code, days=7, source=None):
    """Return chronological [(datetime, mid)] snapshots for a code."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    query = {"code": code, "ts": {"$gte": since}}
    if source:
        query["source"] = source
    else:
        # pick the richest source available for this code
        query["source"] = {"$in": ["liranews", "lirascope_market"]}
    cursor = db.rate_snapshots.find(query, {"_id": 0}).sort("ts", 1)
    rows = await cursor.to_list(5000)
    # prefer a single source for a clean line: liranews then lirascope_market
    for pref in ("liranews", "lirascope_market", "lirascope_cbs"):
        pts = [(datetime.fromisoformat(r["ts"]), r["mid"]) for r in rows
               if r["source"] == pref and r.get("mid") is not None]
        if len(pts) >= 2:
            return pts, pref
    # fallback: any
    pts = [(datetime.fromisoformat(r["ts"]), r["mid"]) for r in rows if r.get("mid") is not None]
    return pts, (rows[0]["source"] if rows else None)
