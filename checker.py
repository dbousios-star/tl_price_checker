"""Throne and Liberty EU potential price checker (questlog.gg -> Discord)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "prices.db"
LOG_PATH = BASE_DIR / "checker.log"
DEBUG_DIR = BASE_DIR / "debug_dump"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STATUS_STYLE = {
    "PRICE DROP": 0x2ECC71,
    "PRICE INCREASE": 0xE67E22,
    "RESTOCKED": 0x3498DB,
    "SOLD OUT": 0xE74C3C,
}

log = logging.getLogger("checker")


class FetchError(Exception):
    pass


# --------------------------------------------------------------------------- setup

def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout.isatty():  # avoid duplicate lines when run_checker.ps1 redirects stdout to checker.log
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- fetching

def unwrap_trpc(payload):
    """tRPC responses look like {"result": {"data": X}} (or a batched list of those)."""
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    data = payload.get("result", {}).get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and "json" in data:  # superjson wrapper
        data = data["json"]
    return data


def fetch_via_api(cfg: dict) -> dict:
    base = cfg["api_base"]
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Referer": cfg["url"]}
    timeout = cfg["browser"]["timeout_ms"] / 1000
    urls = {
        "abilities": base + "getPotentialAbilities?input=" + quote(json.dumps({"language": "en"}, separators=(",", ":"))),
        "board": base + "getPotentialsBoard",
    }
    out = {}
    for name, url in urls.items():
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        out[name] = r.json()
    return out


def fetch_via_browser(cfg: dict, headed: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    bcfg = cfg["browser"]
    captured = {}

    def on_response(resp):
        if resp.status != 200:
            return
        if "getPotentialsBoard" in resp.url:
            captured["board"] = resp
        elif "getPotentialAbilities" in resp.url:
            captured["abilities"] = resp

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bcfg["headless"] and not headed)
        try:
            ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            page.on("response", on_response)
            page.goto(cfg["url"], wait_until="networkidle", timeout=bcfg["timeout_ms"])
            page.wait_for_timeout(bcfg["post_click_wait_ms"])
            missing = {"board", "abilities"} - captured.keys()
            if missing:
                raise FetchError(f"Browser did not capture: {', '.join(sorted(missing))}")
            out = {name: resp.json() for name, resp in captured.items()}
            out["_html"] = page.content()
            return out
        finally:
            browser.close()


def with_retries(fn, attempts: int, delay: float, label: str):
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # requests/HTTP/Playwright timeouts, bad JSON, etc.
            last = e
            log.warning("%s attempt %d/%d failed: %s", label, i, attempts, e)
            if i < attempts:
                time.sleep(delay)
    raise FetchError(f"{label} failed after {attempts} attempts: {last}")


def fetch_data(cfg: dict, force_browser: bool, headed: bool) -> tuple[dict, str]:
    r = cfg["retry"]
    if not (force_browser or headed):
        try:
            return with_retries(lambda: fetch_via_api(cfg), r["max_attempts"], r["delay_seconds"], "API"), "api"
        except FetchError as e:
            log.warning("%s -> falling back to browser", e)
    return with_retries(lambda: fetch_via_browser(cfg, headed), r["max_attempts"], r["delay_seconds"], "Browser"), "browser"


# --------------------------------------------------------------------------- extraction

def extract(raw: dict, target: dict, region_key: str) -> tuple[int | None, int | None]:
    abilities = unwrap_trpc(raw["abilities"]) or []
    board = unwrap_trpc(raw["board"]) or []

    ability = next(
        (a for a in abilities
         if a.get("id") == target["internal_id"] and a.get("kind") == target.get("kind", "skill-enchant")),
        None,
    )
    if ability is None:  # fallback: match by display name + kind
        ability = next(
            (a for a in abilities
             if a.get("name") == target["potential_name"] and a.get("kind") == target.get("kind", "skill-enchant")),
            None,
        )
    if ability is None:
        raise FetchError(f"Potential {target['internal_id']} / {target['potential_name']} not found in abilities list")

    ah_id = ability["auctionHouseId"]
    entry = next((b for b in board if b.get("potentialAbilityId") == ah_id), None)
    region = (entry or {}).get("regions", {}).get(region_key)
    if not region:
        return None, None
    return parse_int(region.get("minPrice")), parse_int(region.get("listings"))


def parse_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).replace(",", "").replace("Lucent", "").strip()
    if s in ("", "-", "–", "N/A"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


# --------------------------------------------------------------------------- db

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS current_state (
            item_key TEXT PRIMARY KEY,
            potential_name TEXT NOT NULL,
            variant TEXT NOT NULL,
            region TEXT NOT NULL,
            last_price INTEGER,
            last_stock INTEGER,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key TEXT NOT NULL,
            price INTEGER,
            stock INTEGER,
            recorded_at TEXT NOT NULL
        );
        """
    )
    return conn


def classify(prev_exists: bool, old: int | None, new: int | None) -> str:
    if not prev_exists:
        return "INITIALIZED"
    if old == new:
        return "UNCHANGED"
    if old is None:
        return "RESTOCKED"
    if new is None:
        return "SOLD OUT"
    return "PRICE DROP" if new < old else "PRICE INCREASE"


# --------------------------------------------------------------------------- discord

def fmt_price(p: int | None) -> str:
    return "Out of stock" if p is None else f"{p:,} Lucent"


def price_line(old: int | None, new: int | None) -> str:
    if old is not None and new is not None:
        d = new - old
        pct = (d / old * 100) if old else 0.0
        return f"{old:,} → {new:,} Lucent ({d:+,} / {pct:+.1f}%)"
    return f"{fmt_price(old)} → {fmt_price(new)}"


def send_discord(payload: dict) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set (copy .env.example to .env)")
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def build_alert(cfg: dict, target: dict, status: str, old, new, stock) -> dict:
    embed = {
        "title": f"{status}: {target['potential_name']} ({target['variant']})",
        "url": cfg["url"],
        "color": STATUS_STYLE[status],
        "fields": [
            {"name": "Price", "value": price_line(old, new), "inline": False},
            {"name": "Listings", "value": "—" if stock is None else str(stock), "inline": True},
            {"name": "Region", "value": cfg["region"], "inline": True},
        ],
        "timestamp": utc_now(),
        "footer": {"text": "questlog.gg auction house"},
    }
    payload = {"embeds": [embed]}
    ping = target.get("max_price_ping")
    if ping is not None and new is not None and new <= ping:
        payload["content"] = "@here"
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    return payload


# --------------------------------------------------------------------------- main flows

def process_target(conn, cfg, target, price, stock) -> None:
    key = f"{cfg['region']}:{target['potential_name']}:{target['variant']}"
    now = utc_now()
    row = conn.execute("SELECT last_price FROM current_state WHERE item_key = ?", (key,)).fetchone()
    old = row[0] if row else None
    status = classify(row is not None, old, price)
    log.info("%s | %s | %s (listings=%s)", key, status, price_line(old, price) if row else fmt_price(price), stock)

    conn.execute(
        """INSERT INTO current_state (item_key, potential_name, variant, region, last_price, last_stock, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(item_key) DO UPDATE SET last_price=excluded.last_price,
               last_stock=excluded.last_stock, updated_at=excluded.updated_at""",
        (key, target["potential_name"], target["variant"], cfg["region"], price, stock, now),
    )
    if status != "UNCHANGED":
        conn.execute(
            "INSERT INTO price_history (item_key, price, stock, recorded_at) VALUES (?, ?, ?, ?)",
            (key, price, stock, now),
        )
    if status in STATUS_STYLE:
        send_discord(build_alert(cfg, target, status, old, price, stock))  # raise -> caller rolls back
    conn.commit()


def save_debug(raw: dict, source: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for name in ("abilities", "board"):
        (DEBUG_DIR / f"{stamp}_{source}_{name}.json").write_text(json.dumps(raw[name], indent=2), encoding="utf-8")
    if "_html" in raw:
        (DEBUG_DIR / f"{stamp}_page.html").write_text(raw["_html"], encoding="utf-8")
    log.info("Debug dump written to %s", DEBUG_DIR)


def send_test_alert(cfg: dict) -> None:
    send_discord({
        "embeds": [{
            "title": "TL Price Checker: test alert",
            "description": "Webhook is working.",
            "url": cfg["url"],
            "color": 0x95A5A6,
            "timestamp": utc_now(),
        }]
    })
    log.info("Test alert sent")


def main() -> int:
    ap = argparse.ArgumentParser(description="TL EU potential price checker")
    ap.add_argument("--headed", action="store_true", help="use a visible browser (forces browser path)")
    ap.add_argument("--debug", action="store_true", help="dump raw data; no DB writes or alerts")
    ap.add_argument("--test-alert", action="store_true", help="send a test Discord embed and exit")
    ap.add_argument("--force-browser", action="store_true", help="skip the API and use headless Playwright")
    args = ap.parse_args()

    setup_logging()
    load_dotenv(BASE_DIR / ".env")
    cfg = load_config()

    try:
        if args.test_alert:
            send_test_alert(cfg)
            return 0

        raw, source = fetch_data(cfg, args.force_browser, args.headed)
        results = [(t, *extract(raw, t, cfg["region_key"])) for t in cfg["targets"]]

        if args.debug:
            save_debug(raw, source)
            for t, price, stock in results:
                log.info("[debug/%s] %s (%s) %s: price=%s listings=%s",
                         source, t["potential_name"], t["variant"], cfg["region"], price, stock)
            return 0

        conn = db_connect()
        try:
            for t, price, stock in results:
                try:
                    process_target(conn, cfg, t, price, stock)
                except Exception:
                    conn.rollback()
                    raise
        finally:
            conn.close()
        return 0
    except Exception as e:
        log.error("Check failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
