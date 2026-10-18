"""
daraz tracker collector: snapshots Daraz category listings into SQLite, to test whether
11.11 sale discounts are real or whether prices were raised beforehand.

Why price bands: Daraz's feed answers "page 2, 3..." with page 1 again (found 2026-10-17,
see probe_paging.py). Page 1 with a price filter always works. So each category is cut into
price bands, and we only ever ask for page 1 of each band (up to 40 listings per band).

    First sweep (once per category): DISCOVER bands. Start with the whole price range, keep
        splitting the fullest band in half (on a log scale, prices are skewed) until every band
        holds <= 40 listings or the category has MAX_BANDS bands. Saved to data/bands.json and
        then FIXED for the whole study, so every sweep covers the same slices.
    Every sweep: page 1 of every band -> raw feed saved (gzipped) -> listings stored.

Small categories end up fully covered (every band <= 40 listings). Big ones get the top 40
listings (Daraz's default ranking) in each of MAX_BANDS price bands.

This file only collects; it never analyses.

Usage:
    python collector.py                 # one sweep now, then exit
    python collector.py --loop 21600    # keep running: a sweep every 6 hours (start_collector.bat)
    python collector.py --test          # quick check: 2 categories, 6 bands, separate data_test/ folder
    python collector.py --reparse       # rebuild the database from saved raw files (after a parser fix)
"""

import argparse
import gzip
import json
import logging
import math
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


# ---------------------------------------------------------------- settings

# category slug (daraz.pk/<slug>/) -> (label, group). The groups are a hypothesis to test, not a fact.
CATEGORIES = {
    "smartphones":              ("Smartphones",               "tight-margin"),
    "traditional-laptops":      ("Laptops",                   "tight-margin"),
    "refrigerators":            ("Refrigerators",             "tight-margin"),
    "smart-watches":            ("Smart watches",             "discount-heavy"),
    "wireless-earbuds":         ("Wireless earbuds",          "discount-heavy"),
    "small-kitchen-appliances": ("Small kitchen appliances",  "discount-heavy"),
    "mens-sneakers":            ("Men's sneakers",            "discount-heavy"),
    "womens-lawn":              ("Women's lawn / unstitched", "discount-heavy"),
    "face-mask-&-packs":        ("Face masks & packs",        "discount-heavy"),
    "oil-ghee":                 ("Oil & ghee",                "control"),
}
TEST_CATEGORIES = ["smartphones", "womens-lawn"]

FEED_URL = "https://www.daraz.pk/{slug}/?ajax=true&page=1&price={lo}-{hi}"
PAGE_URL = "https://www.daraz.pk/{slug}/"

PAGE_SIZE = 40              # listings Daraz returns per page
MAX_BANDS = 30              # per category -> at most ~1,200 listings and 30 requests per category per sweep
PRICE_MIN, PRICE_MAX = 1, 20_000_000   # Rs; the whole range a band can cover
MAX_DISCOVERY_REQUESTS = 80 # per category, safety stop for band discovery
GAP_BETWEEN_REQUESTS = 6    # seconds; the pace Site Scout tested as safe
REQUEST_TIMEOUT = 30
RETRY_WAIT = 30             # one retry per request after this many seconds
MAX_FAILED_IN_A_ROW = 3     # then assume we're blocked and stop the whole sweep
OUT_OF_BAND_LIMIT = 0.2     # >20% of a band's listings priced outside it = the price filter was ignored
REPEAT_LIMIT = 0.8          # a band where 80%+ of listings were already seen this sweep = bad answer
LOCK_PORT = 47510           # held while a loop runs, so two loops can't run at once

ROOT = Path(__file__).resolve().parent
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
SOURCE = "pc"               # which device took the snapshot


class Paths:
    """All file locations, so --test can point everything at a separate folder."""
    def __init__(self, data_dir):
        self.data = data_dir
        self.raw = data_dir / "raw"
        self.db = data_dir / "daraz.db"
        self.log = data_dir / "collector.log"
        self.bands = data_dir / "bands.json"


P = Paths(ROOT / "data")


# ---------------------------------------------------------------- database

SCHEMA = """
-- One row per sweep (all categories). Failed and partial sweeps too, so gaps are visible.
CREATE TABLE IF NOT EXISTS runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,       -- UTC, ISO 8601
    finished_at    TEXT,
    status         TEXT NOT NULL,       -- 'running', 'ok', 'partial', 'blocked', 'error: ...'
    bands_ok       INTEGER DEFAULT 0,
    bands_failed   INTEGER DEFAULT 0,
    listings       INTEGER DEFAULT 0,
    source         TEXT
);

-- One row per band requested per sweep, so coverage can be checked.
CREATE TABLE IF NOT EXISTS bands (
    run_id         INTEGER NOT NULL,
    category       TEXT NOT NULL,
    band           INTEGER NOT NULL,    -- index in bands.json (0 = cheapest)
    price_lo       INTEGER NOT NULL,
    price_hi       INTEGER NOT NULL,
    fetched_at     TEXT NOT NULL,
    status         TEXT NOT NULL,       -- 'ok' or 'error: ...'
    item_count     INTEGER,             -- listings returned (max 40)
    band_total     INTEGER,             -- listings Daraz says are in the band (> 40 = we got the top 40)
    raw_path       TEXT,
    PRIMARY KEY (run_id, category, band)
);

-- Facts about a listing that rarely change. One row per listing, updated to the latest values.
CREATE TABLE IF NOT EXISTS products (
    item_id        INTEGER PRIMARY KEY,
    category       TEXT NOT NULL,
    name           TEXT,
    brand          TEXT,
    seller_id      INTEGER,
    seller_name    TEXT,
    location       TEXT,
    item_url       TEXT,
    first_seen_run INTEGER NOT NULL,
    last_seen_run  INTEGER NOT NULL
);

-- What changes between sweeps. One row per listing per sweep per category.
CREATE TABLE IF NOT EXISTS snapshots (
    run_id          INTEGER NOT NULL,
    item_id         INTEGER NOT NULL,
    category        TEXT NOT NULL,
    band            INTEGER,
    position        INTEGER,            -- 1-based rank inside its band this sweep
    price           REAL,               -- Rs (can have paisa, e.g. 331.1), what the buyer pays before vouchers
    original_price  REAL,               -- Rs, the crossed-out price; NULL = no discount shown
    discount_pct    INTEGER,            -- Daraz's own "6% Off" label, as a number
    sku_id          INTEGER,            -- the variant the price belongs to (cheapest one)
    in_stock        INTEGER,            -- 1/0
    sponsored       INTEGER,            -- 1/0 (paid placement)
    rating          REAL,
    reviews         INTEGER,
    PRIMARY KEY (run_id, item_id, category)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_item ON snapshots (item_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at);
"""


def open_db():
    P.data.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(P.db, timeout=60)
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- parse

def to_int(value):
    """'79499' -> 79499, '6% Off' -> 6, '' / None -> None."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_money(value):
    """'79499' -> 79499, '331.1' -> 331.1, '1,299' -> 1299, '' / None -> None.

    Daraz sends some prices with paisa ('331.1'). to_int would strip the dot and read 3311,
    so prices must never go through to_int (bug found in sweep 1, 2026-10-17).
    """
    if value in (None, ""):
        return None
    try:
        x = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return int(x) if x == int(x) else round(x, 2)


class NotAFeed(Exception):
    """The answer wasn't the product feed: a block page, captcha, or a changed site."""


def parse_feed(text):
    """Return (listings, total) from one feed answer. Raises NotAFeed if it isn't one."""
    try:
        data = json.loads(text)
    except ValueError:
        raise NotAFeed("answer is not JSON (block page or captcha?)")
    mods = data.get("mods") if isinstance(data, dict) else None
    if not isinstance(mods, dict) or "listItems" not in mods:
        raise NotAFeed("JSON without mods.listItems (block, or the feed changed)")

    # With a filter on, the filter box holds the filtered count (sic: Daraz's spelling).
    total = to_int((mods.get("filter") or {}).get("filteredQuatity"))
    if total is None:
        total = to_int((data.get("mainInfo") or {}).get("totalResults"))

    listings = []
    for it in mods["listItems"] or []:
        item_id = to_int(it.get("itemId") or it.get("nid"))
        if item_id is None:
            continue  # without an id we can't follow it across sweeps
        url = it.get("itemUrl") or ""
        listings.append({
            "item_id": item_id,
            "name": it.get("name"),
            "brand": it.get("brandName"),
            "seller_id": to_int(it.get("sellerId")),
            "seller_name": it.get("sellerName"),
            "location": it.get("location"),
            "item_url": ("https:" + url) if url.startswith("//") else url,
            "price": to_money(it.get("price")),
            "original_price": to_money(it.get("originalPrice")),
            "discount_pct": to_int(it.get("discount")),
            "sku_id": to_int(it.get("skuId")),
            "in_stock": 1 if it.get("inStock") else 0,
            "sponsored": 1 if it.get("isSponsored") else 0,
            "rating": to_float(it.get("ratingScore")),
            "reviews": to_int(it.get("review")),
        })
    if total is None:
        total = len(listings)
    return listings, total


# ---------------------------------------------------------------- polite fetching

class Fetcher:
    """The only door to Daraz: spacing, one retry, and the blocked-detector live here."""

    def __init__(self, session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self.sleep = sleep
        self.first = True
        self.failed_in_a_row = 0
        self.requests = 0

    def band(self, slug, lo, hi):
        """(text, listings, total) for page 1 of a price band. Raises after a failed retry."""
        error = None
        for attempt in (1, 2):
            if not self.first:
                self.sleep(GAP_BETWEEN_REQUESTS if attempt == 1 else RETRY_WAIT)
            self.first = False
            self.requests += 1
            try:
                resp = self.session.get(FEED_URL.format(slug=slug, lo=lo, hi=hi),
                                        headers={"Referer": PAGE_URL.format(slug=slug)},
                                        timeout=REQUEST_TIMEOUT)
                if resp.status_code == 429:
                    raise NotAFeed("HTTP 429 (too many requests)")
                resp.raise_for_status()
                listings, total = parse_feed(resp.text)
                self.failed_in_a_row = 0
                return resp.text, listings, total
            except Exception as e:
                error = e
                log.warning("%s %s-%s attempt %s failed: %s", slug, lo, hi, attempt, e)
        self.failed_in_a_row += 1
        raise error

    @property
    def blocked(self):
        return self.failed_in_a_row >= MAX_FAILED_IN_A_ROW


class Blocked(Exception):
    pass


# ---------------------------------------------------------------- band discovery

def log_mid(lo, hi):
    """A split point between lo and hi on a log scale (prices are skewed: many cheap, few expensive)."""
    m = int(round(math.sqrt(max(lo, 1) * hi)))
    return min(max(m, lo), hi - 1)


def discover_bands(fetcher, slug, max_bands=MAX_BANDS):
    """Cut a category's price range into bands of <= PAGE_SIZE listings (or max_bands bands).

    Each band is [lo, hi, items_lo, items_hi, count]: it COVERS lo..hi (bands are contiguous, so a
    listing whose price moves later still lands in some band), and its listings were seen inside
    items_lo..items_hi, which is where the next split goes. Splitting costs one request: we count
    the left half and get the right half by subtraction.
    """
    _, _, total = fetcher.band(slug, PRICE_MIN, PRICE_MAX)
    bands = [[PRICE_MIN, PRICE_MAX, PRICE_MIN, PRICE_MAX, total]]
    stuck = set()          # bands that can't be split further (one price holds > 40 listings)
    requests_used = 1

    while len(bands) < max_bands and requests_used < MAX_DISCOVERY_REQUESTS:
        if fetcher.blocked:
            raise Blocked("blocked during band discovery")
        candidates = [b for b in bands if b[4] > PAGE_SIZE and (b[0], b[1]) not in stuck]
        if not candidates:
            break
        b = max(candidates, key=lambda x: x[4])
        lo, hi, ilo, ihi, count = b
        if ihi - ilo < 1:
            stuck.add((lo, hi))
            continue
        m = log_mid(ilo, ihi)
        try:
            _, _, left = fetcher.band(slug, ilo, m)
        except Exception:
            requests_used += 1
            stuck.add((lo, hi))  # couldn't count this split; keep the band as it is
            continue
        requests_used += 1
        right = max(count - left, 0)
        bands.remove(b)
        if left and right:
            bands += [[lo, m, ilo, m, left], [m + 1, hi, m + 1, ihi, right]]
        elif left:   # everything is in the left part: same coverage, narrower search area
            bands.append([lo, hi, ilo, m, left])
        else:        # everything is in the right part
            bands.append([lo, hi, m + 1, ihi, right])

    bands.sort(key=lambda x: x[0])
    log.info("%-26s %s listings -> %s bands (%s requests); %s bands still over %s",
             slug, total, len(bands), requests_used, sum(1 for b in bands if b[4] > PAGE_SIZE), PAGE_SIZE)
    return {"total_at_discovery": total, "discovered_at": utc_now().isoformat(),
            "bands": [[b[0], b[1], b[4]] for b in bands]}   # [lo, hi, count at discovery]


def load_bands():
    try:
        return json.loads(P.bands.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_bands(all_bands):
    P.data.mkdir(parents=True, exist_ok=True)
    tmp = P.bands.with_suffix(".tmp")
    tmp.write_text(json.dumps(all_bands, indent=1), encoding="utf-8")
    tmp.replace(P.bands)


# ---------------------------------------------------------------- store

def save_raw(text, run_id, slug, band):
    folder = P.raw / f"run{run_id:05d}"
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9\-]", "_", slug)
    path = folder / f"{safe}_b{band:02d}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)
    return path


def store_band(conn, run_id, slug, band, lo, hi, fetched_at, listings, total, raw_path):
    with conn:  # one transaction per band
        conn.execute(
            "INSERT OR REPLACE INTO bands (run_id, category, band, price_lo, price_hi, fetched_at, status, "
            "item_count, band_total, raw_path) VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?)",
            (run_id, slug, band, lo, hi, fetched_at, len(listings), total, str(raw_path)),
        )
        for i, o in enumerate(listings, start=1):
            conn.execute(
                """
                INSERT INTO products (item_id, category, name, brand, seller_id, seller_name, location, item_url,
                                      first_seen_run, last_seen_run)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (item_id) DO UPDATE SET
                    name = excluded.name, brand = excluded.brand, seller_id = excluded.seller_id,
                    seller_name = excluded.seller_name, location = excluded.location,
                    item_url = excluded.item_url,
                    last_seen_run = MAX(last_seen_run, excluded.last_seen_run)
                """,
                (o["item_id"], slug, o["name"], o["brand"], o["seller_id"], o["seller_name"],
                 o["location"], o["item_url"], run_id, run_id),
            )
            # A price can change between two band requests, moving a listing into a second band: keep the first.
            conn.execute(
                "INSERT OR IGNORE INTO snapshots (run_id, item_id, category, band, position, price, original_price, "
                "discount_pct, sku_id, in_stock, sponsored, rating, reviews) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, o["item_id"], slug, band, i, o["price"], o["original_price"], o["discount_pct"],
                 o["sku_id"], o["in_stock"], o["sponsored"], o["rating"], o["reviews"]),
            )


def store_band_failure(conn, run_id, slug, band, lo, hi, fetched_at, error, raw_path=None):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO bands (run_id, category, band, price_lo, price_hi, fetched_at, status, raw_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, slug, band, lo, hi, fetched_at, f"error: {error}"[:300], str(raw_path) if raw_path else None),
        )


def check_band(listings, lo, hi, seen_ids):
    """Return an error message if this answer can't be trusted, else None."""
    if not listings:
        return None
    priced = [o for o in listings if o["price"] is not None]
    outside = sum(1 for o in priced if not lo <= o["price"] <= hi)
    if priced and outside > OUT_OF_BAND_LIMIT * len(priced):
        return f"price filter ignored ({outside}/{len(priced)} listings outside {lo}-{hi})"
    ids = {o["item_id"] for o in listings}
    repeated = len(ids & seen_ids)
    if repeated >= REPEAT_LIMIT * len(ids) and len(ids) >= 5:
        return f"repeated answer ({repeated}/{len(ids)} listings already seen this sweep)"
    return None


# ---------------------------------------------------------------- one sweep

def utc_now():
    return datetime.now(timezone.utc)


def run_sweep(categories=None, max_bands=MAX_BANDS, session=None, sleep=time.sleep):
    """Collect every category once. Returns the final status ('ok', 'partial', 'blocked', 'error: ...')."""
    categories = categories or list(CATEGORIES)
    fetcher = Fetcher(session, sleep)
    conn = open_db()
    with conn:
        run_id = conn.execute("INSERT INTO runs (started_at, status, source) VALUES (?, 'running', ?)",
                              (utc_now().isoformat(), SOURCE)).lastrowid

    all_bands = load_bands()
    bands_ok = bands_failed = listings_total = 0
    status = "ok"
    try:
        for slug in categories:
            if slug not in all_bands:
                log.info("%s: no bands yet, discovering (one time)", slug)
                all_bands[slug] = discover_bands(fetcher, slug, max_bands)
                save_bands(all_bands)

            seen_ids = set()
            cat_items = cat_capped = 0
            for i, (lo, hi, _) in enumerate(all_bands[slug]["bands"]):
                if fetcher.blocked:
                    raise Blocked(f"{MAX_FAILED_IN_A_ROW} requests failed in a row")
                fetched_at = utc_now().isoformat()
                raw_path = None
                try:
                    text, listings, total = fetcher.band(slug, lo, hi)
                    raw_path = save_raw(text, run_id, slug, i)
                    problem = check_band(listings, lo, hi, seen_ids)
                    if problem:
                        raise NotAFeed(problem)
                except Exception as e:
                    store_band_failure(conn, run_id, slug, i, lo, hi, fetched_at, e, raw_path)
                    bands_failed += 1
                    continue
                seen_ids |= {o["item_id"] for o in listings}
                store_band(conn, run_id, slug, i, lo, hi, fetched_at, listings, total, raw_path)
                bands_ok += 1
                cat_items += len(listings)
                cat_capped += total > len(listings)
                listings_total += len(listings)

            log.info("%-26s %5s listings from %s bands (%s bands had more than we could see)",
                     slug, cat_items, len(all_bands[slug]["bands"]), cat_capped)

        if bands_failed:
            status = "partial"
        return status

    except Blocked as e:
        status = "blocked"
        log.error("%s: stopping this sweep to stay polite", e)
        return status
    except Exception as e:
        status = f"error: {e}"[:300]
        log.exception("sweep crashed: %s", e)
        return status
    finally:
        with conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, bands_ok = ?, bands_failed = ?, listings = ? "
                "WHERE run_id = ?",
                (utc_now().isoformat(), status, bands_ok, bands_failed, listings_total, run_id),
            )
        conn.close()
        log.info("sweep %s finished: %s | %s bands ok, %s failed | %s listings | %s requests",
                 run_id, status, bands_ok, bands_failed, listings_total, fetcher.requests)


# ---------------------------------------------------------------- re-read raw files

def reparse(run_ids=None):
    """Rebuild the database rows of finished sweeps from their saved raw feeds.

    Use after fixing a parsing bug: nothing has to be downloaded again. Band rows for requests
    that never got an answer (network errors, no raw file) are kept as they are.
    Returns the number of sweeps rebuilt.
    """
    all_bands = load_bands()
    conn = open_db()
    rebuilt = 0
    try:
        if run_ids is None:
            run_ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE status != 'running' ORDER BY run_id")]
        for run_id in run_ids:
            folder = P.raw / f"run{run_id:05d}"
            if not folder.exists():
                continue
            fetched = {(c, b): t for c, b, t in conn.execute(
                "SELECT category, band, fetched_at FROM bands WHERE run_id = ?", (run_id,))}
            with conn:
                conn.execute("DELETE FROM snapshots WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM bands WHERE run_id = ? AND raw_path IS NOT NULL", (run_id,))
            for slug in CATEGORIES:
                if slug not in all_bands:
                    continue
                safe = re.sub(r"[^a-z0-9\-]", "_", slug)
                seen_ids = set()
                for i, (lo, hi, _) in enumerate(all_bands[slug]["bands"]):
                    path = folder / f"{safe}_b{i:02d}.json.gz"
                    if not path.exists():
                        continue
                    fetched_at = fetched.get((slug, i)) or utc_now().isoformat()
                    try:
                        with gzip.open(path, "rt", encoding="utf-8") as f:
                            listings, total = parse_feed(f.read())
                        problem = check_band(listings, lo, hi, seen_ids)
                        if problem:
                            raise NotAFeed(problem)
                    except Exception as e:
                        store_band_failure(conn, run_id, slug, i, lo, hi, fetched_at, e, path)
                        continue
                    seen_ids |= {o["item_id"] for o in listings}
                    store_band(conn, run_id, slug, i, lo, hi, fetched_at, listings, total, path)
            ok, failed = conn.execute(
                "SELECT SUM(status = 'ok'), SUM(status != 'ok') FROM bands WHERE run_id = ?", (run_id,)).fetchone()
            n = conn.execute("SELECT COUNT(*) FROM snapshots WHERE run_id = ?", (run_id,)).fetchone()[0]
            with conn:
                conn.execute(
                    "UPDATE runs SET bands_ok = ?, bands_failed = ?, listings = ?, "
                    "status = CASE WHEN status IN ('ok', 'partial') THEN (CASE WHEN ? > 0 THEN 'partial' ELSE 'ok' END) "
                    "ELSE status END WHERE run_id = ?",
                    (ok or 0, failed or 0, n, failed or 0, run_id))
            log.info("re-read sweep %s from raw files: %s bands ok, %s failed, %s listings", run_id, ok or 0, failed or 0, n)
            rebuilt += 1
    finally:
        conn.close()
    return rebuilt


# ---------------------------------------------------------------- loop

def last_good_sweep_start():
    """When the last ok/partial sweep started (UTC), or None. So a PC restart doesn't double-collect."""
    conn = open_db()
    try:
        row = conn.execute("SELECT MAX(started_at) FROM runs WHERE status IN ('ok', 'partial')").fetchone()
    finally:
        conn.close()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def acquire_single_instance_lock():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return None
    return lock  # must stay referenced, or Python frees the port


def run_loop(interval):
    lock = acquire_single_instance_lock()
    if lock is None:
        log.info("another collector loop is already running; exiting")
        return
    log.info("loop started: a sweep every %s hours", round(interval / 3600, 2))
    # A sweep left as 'running' was cut off (PC shut down, or stopped from the panel). Only one
    # loop can run at a time (the lock above), so nothing else can be mid-sweep right now.
    conn = open_db()
    with conn:
        n = conn.execute("UPDATE runs SET status = 'interrupted', finished_at = COALESCE(finished_at, ?) "
                         "WHERE status = 'running'", (utc_now().isoformat(),)).rowcount
    conn.close()
    if n:
        log.info("marked %s cut-off sweep(s) as interrupted", n)
    backoff = 0
    while True:
        last = last_good_sweep_start()
        due = (last + timedelta(seconds=interval)) if last else utc_now()
        if backoff:
            due = max(due, utc_now() + timedelta(seconds=backoff))
        wait = (due - utc_now()).total_seconds()
        if wait > 0:
            time.sleep(min(wait, 300))  # wake every 5 min, so a laptop that slept catches up promptly
            continue
        status = run_sweep()
        # If Daraz pushed back, wait longer each time (1 h, 2 h, 4 h ... max 12 h) instead of hammering.
        backoff = min(max(backoff * 2, 3600), 12 * 3600) if status == "blocked" else 0


# ---------------------------------------------------------------- main

log = logging.getLogger("daraz")


def setup_logging():
    P.data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(P.log, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )


def main():
    global P
    p = argparse.ArgumentParser(description="Snapshot Daraz category listings into SQLite.")
    p.add_argument("--loop", type=int, metavar="SECONDS", help="keep running, one sweep every SECONDS (e.g. 21600)")
    p.add_argument("--reparse", action="store_true",
                   help="rebuild all finished sweeps from their saved raw files (after a parser fix)")
    p.add_argument("--test", action="store_true",
                   help="quick check: 2 categories, 6 bands each, in a separate data_test/ folder")
    a = p.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    if a.test:
        P = Paths(ROOT / "data_test")
        setup_logging()
        status = run_sweep(TEST_CATEGORIES, max_bands=6)
        sys.exit(0 if status == "ok" else 1)

    setup_logging()
    if a.reparse:
        n = reparse()
        log.info("re-read %s sweeps", n)
        return
    if a.loop:
        run_loop(a.loop)
    else:
        sys.exit(0 if run_sweep() in ("ok", "partial") else 1)


if __name__ == "__main__":
    main()
