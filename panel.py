"""
daraz tracker control panel: one place to check, start, stop and repair the collector.

Double-click panel.bat (or run:  .venv\\Scripts\\python panel.py)

    1  Status            running or not, what it's doing right now, last/next sweep, disk use
    2  Last sweep        per category: listings, bands ok/failed, bands where we only see the top 40
    3  Start collector   same as double-clicking start_collector.bat
    4  Stop collector    ends the background loop (no Task Manager needed)
    5  Show log          last lines of data/collector.log
    6  Re-read raw data  rebuild the database from saved raw feeds (after a parser fix), then restart
    0  Exit

Stopping lasts until the next login: the Startup-folder shortcut starts the collector again.
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "daraz.db"
RAW_DIR = DATA_DIR / "raw"
LOG_PATH = DATA_DIR / "collector.log"
START_BAT = ROOT / "start_collector.bat"
LOCK_PORT = 47510           # must match collector.py: the running loop holds this port
SWEEP_EVERY_H = 6           # must match start_collector.bat (--loop 21600)
BANDS_PER_SWEEP = 300       # 10 categories x 30 bands


# ---------------------------------------------------------------- helpers

def mb(n_bytes):
    return f"{n_bytes / 1e6:,.1f} MB"


def ask(prompt, default=""):
    try:
        answer = input(prompt).strip()
    except EOFError:
        return default
    return answer or default


def pause():
    ask("\npress Enter to go back to the menu ")


def local(iso):
    return datetime.fromisoformat(iso).astimezone()


def ago(iso):
    minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60
    return f"{minutes:,.0f} min ago" if minutes < 120 else f"{minutes / 60:,.1f} h ago"


def folder_size(path):
    files = [f for f in path.rglob("*") if f.is_file()] if path.exists() else []
    return sum(f.stat().st_size for f in files), len(files)


def open_db():
    # timeout: if the collector is mid-write, wait for it instead of failing
    return sqlite3.connect(DB_PATH, timeout=60)


# ---------------------------------------------------------------- collector process

def collector_running():
    """The collector loop holds LOCK_PORT for as long as it lives. If we can't bind it, it's running."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        return False
    except OSError:
        return True
    finally:
        s.close()


def collector_pids():
    """Process ids of python(w) processes running this folder's 'collector.py --loop'."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Where-Object { $_.CommandLine -like '*daraz_tracker*collector.py*--loop*' } | "
          "Select-Object -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def current_sweep():
    """(started_at, bands done) of the sweep in progress, or None."""
    if not DB_PATH.exists() or not collector_running():
        return None
    conn = open_db()
    try:
        row = conn.execute("SELECT run_id, started_at FROM runs WHERE status = 'running' "
                           "ORDER BY run_id DESC LIMIT 1").fetchone()
        if not row:
            return None
        done = conn.execute("SELECT COUNT(*) FROM bands WHERE run_id = ?", (row[0],)).fetchone()[0]
        return row[1], done
    finally:
        conn.close()


def state_line():
    if not collector_running():
        return "STOPPED"
    sweep = current_sweep()
    if sweep:
        return f"RUNNING, sweeping now ({sweep[1]}/{BANDS_PER_SWEEP} bands, started {local(sweep[0]):%H:%M})"
    return "RUNNING, waiting for the next sweep"


def stop_collector(quiet=False):
    if not collector_running():
        if not quiet:
            print("The collector isn't running.")
        return True
    if current_sweep() and not quiet:
        if not ask("A sweep is in progress. Stop anyway? It will be marked 'interrupted'. [y/N] ").lower().startswith("y"):
            print("Left it running.")
            return False
    pids = collector_pids()
    if not pids:
        print("The collector is running, but its process couldn't be found. Use Task Manager → pythonw.exe.")
        return False
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    for _ in range(10):
        if not collector_running():
            if not quiet:
                print("Collector stopped. It starts again at your next login (Startup shortcut), or with option 3.")
            return True
        time.sleep(0.5)
    print("Tried to stop it, but it still seems to be running. Check Task Manager → pythonw.exe.")
    return False


def start_collector(quiet=False):
    if collector_running():
        if not quiet:
            print("The collector is already running.")
        return True
    if not START_BAT.exists():
        print(f"Can't find {START_BAT.name}.")
        return False
    subprocess.Popen(["cmd", "/c", str(START_BAT)], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(20):
        if collector_running():
            if not quiet:
                print(f"Collector started. It sweeps now if the last sweep is {SWEEP_EVERY_H}+ hours old, "
                      "otherwise it waits.")
            return True
        time.sleep(0.5)
    print("Started it, but it doesn't look like it's running. Check data/collector.log.")
    return False


# ---------------------------------------------------------------- 1 status

def status():
    print("Collector:", state_line())
    if not DB_PATH.exists():
        print("No database yet: start the collector.")
        return
    conn = open_db()
    try:
        last = conn.execute("SELECT started_at, status, listings, bands_ok, bands_failed FROM runs "
                            "WHERE status != 'running' ORDER BY run_id DESC LIMIT 1").fetchone()
        last_good = conn.execute("SELECT MAX(started_at) FROM runs WHERE status IN ('ok', 'partial')").fetchone()[0]
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        good24, bad24 = conn.execute(
            "SELECT SUM(status IN ('ok', 'partial')), SUM(status NOT IN ('ok', 'partial', 'running')) "
            "FROM runs WHERE started_at >= ?", (since,)).fetchone()
        n_sweeps, first = conn.execute(
            "SELECT COUNT(*), MIN(started_at) FROM runs WHERE status IN ('ok', 'partial')").fetchone()
        n_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    finally:
        conn.close()

    if last:
        print(f"Last finished sweep: {local(last[0]):%a %d %b %H:%M} ({ago(last[0])}): {last[1].upper()}, "
              f"{last[2]:,} listings, {last[3]} bands ok, {last[4]} failed")
    if last_good and collector_running() and not current_sweep():
        due = datetime.fromisoformat(last_good) + timedelta(hours=SWEEP_EVERY_H)
        print(f"Next sweep due: {due.astimezone():%a %H:%M}" +
              (" (overdue: starts within 5 min)" if due < datetime.now(timezone.utc) else ""))
    print(f"Last 24 h: {good24 or 0} good sweeps ({24 // SWEEP_EVERY_H} if the PC never slept), {bad24 or 0} failed")
    if first:
        print(f"Collecting since: {local(first):%a %d %b %H:%M}, {n_sweeps} good sweeps, "
              f"{n_products:,} different listings seen")
    raw_bytes, raw_n = folder_size(RAW_DIR)
    print(f"Database: {mb(DB_PATH.stat().st_size)} | raw feeds: {raw_n:,} files, {mb(raw_bytes)}")
    print(f"Free space on this drive: {shutil.disk_usage(ROOT).free / 1e9:,.1f} GB")


# ---------------------------------------------------------------- 2 last sweep

def last_sweep():
    if not DB_PATH.exists():
        print("No database yet.")
        return
    conn = open_db()
    try:
        run = conn.execute("SELECT run_id, started_at, finished_at, status FROM runs "
                           "WHERE status != 'running' ORDER BY run_id DESC LIMIT 1").fetchone()
        if not run:
            print("No finished sweep yet.")
            return
        rows = conn.execute(
            "SELECT category, SUM(status = 'ok'), SUM(status != 'ok'), SUM(COALESCE(item_count, 0)), "
            "SUM(status = 'ok' AND band_total > item_count) FROM bands WHERE run_id = ? GROUP BY category",
            (run[0],)).fetchall()
        failures = conn.execute("SELECT category, band, price_lo, price_hi, status FROM bands "
                                "WHERE run_id = ? AND status != 'ok'", (run[0],)).fetchall()
    finally:
        conn.close()

    took = (datetime.fromisoformat(run[2]) - datetime.fromisoformat(run[1])).total_seconds() / 60 if run[2] else 0
    print(f"Sweep {run[0]}: {local(run[1]):%a %d %b %H:%M}, took {took:.0f} min, {run[3].upper()}\n")
    print(f"  {'category':26} {'listings':>8} {'bands ok':>9} {'failed':>7} {'top-40 only':>12}")
    for cat, ok, bad, items, capped in rows:
        print(f"  {cat:26} {items:8,} {ok:9} {bad or 0:7} {capped or 0:12}")
    print("\n  'top-40 only' = bands holding more than 40 listings, where we see Daraz's top 40.")
    if failures:
        print("\nFailed bands:")
        for cat, band, lo, hi, st in failures:
            print(f"  {cat} band {band} (Rs {lo:,}-{hi:,}): {st[:110]}")


# ---------------------------------------------------------------- 5 log

def show_log(lines=25):
    if not LOG_PATH.exists():
        print("No log yet.")
        return
    with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
        tail = f.readlines()[-lines:]
    print("".join(tail).rstrip())


# ---------------------------------------------------------------- 6 re-read raw data

def reread():
    print("Re-read raw data rebuilds every finished sweep's database rows from the saved raw feeds.")
    print("Use it after a parser fix. Nothing is downloaded. Takes about a minute per 10 sweeps.")
    if not ask("\nGo ahead? [y/N] ").lower().startswith("y"):
        print("Cancelled.")
        return
    was_running = collector_running()
    if was_running:
        if current_sweep():
            print("A sweep is in progress. Wait for it to finish (option 1 shows progress), then try again.")
            return
        print("Pausing the collector...")
        if not stop_collector(quiet=True):
            return
    try:
        subprocess.run([sys.executable, str(ROOT / "collector.py"), "--reparse"], cwd=ROOT)
    finally:
        if was_running:
            start_collector(quiet=True)
            print("Collector restarted (with the current code).")


# ---------------------------------------------------------------- menu

MENU = [
    ("1", "Status", status),
    ("2", "Last sweep", last_sweep),
    ("3", "Start collector", start_collector),
    ("4", "Stop collector", stop_collector),
    ("5", "Show log", show_log),
    ("6", "Re-read raw data", reread),
]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(f"daraz tracker control panel\ncollector: {state_line()}\n")
        for key, label, _ in MENU:
            print(f"  {key}  {label}")
        print("  0  Exit\n")
        choice = ask("> ")
        if choice in ("0", "q", "exit"):
            return
        action = next((fn for key, _, fn in MENU if key == choice), None)
        if action is None:
            continue
        print()
        try:
            action()
        except Exception as e:  # keep the panel open whatever goes wrong
            print(f"Error: {e}")
        pause()


if __name__ == "__main__":
    main()
