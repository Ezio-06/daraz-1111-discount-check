"""Pulls the numbers behind the README charts out of data/daraz.db into docs/early_numbers.json.

Uses one finished sweep (the latest 'ok' one unless --run is given). Only reads the database.
Run: python tools/early_numbers.py [--run N]
"""
import argparse
import json
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import sys
sys.path.insert(0, str(ROOT))
from collector import CATEGORIES  # noqa: E402  (labels and groups live in one place)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=int, help="sweep id (default: latest ok sweep)")
    a = p.parse_args()
    conn = sqlite3.connect(ROOT / "data" / "daraz.db")
    run_id = a.run or conn.execute("SELECT MAX(run_id) FROM runs WHERE status = 'ok'").fetchone()[0]
    started = conn.execute("SELECT started_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]

    out = {"run_id": run_id, "started_at": started, "categories": {}}
    for slug, (label, group) in CATEGORIES.items():
        rows = conn.execute("SELECT price, original_price, discount_pct FROM snapshots "
                            "WHERE run_id = ? AND category = ?", (run_id, slug)).fetchall()
        shown = [r for r in rows if r[1] and r[1] > r[0]]          # a crossed-out price above the price
        pct = [r[2] for r in shown if r[2] is not None]
        seen, in_bands, capped = conn.execute(
            "SELECT SUM(item_count), SUM(band_total), SUM(band_total > item_count) FROM bands "
            "WHERE run_id = ? AND category = ? AND status = 'ok'", (run_id, slug)).fetchone()
        out["categories"][slug] = {
            "label": label, "group": group, "listings": len(rows),
            "share_discounted": round(100 * len(shown) / len(rows), 1) if rows else None,
            "median_discount": statistics.median(pct) if pct else None,
            "share_50_plus": round(100 * sum(x >= 50 for x in pct) / len(rows), 1) if rows else None,
            "seen": seen, "in_bands": in_bands, "bands_top40_only": capped,
        }
    path = ROOT / "docs" / "early_numbers.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {path} from sweep {run_id} ({started})")


if __name__ == "__main__":
    main()
