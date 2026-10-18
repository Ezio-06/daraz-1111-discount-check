"""
One-off probe: why does page 2 of the Daraz feed sometimes come back as page 1?
Makes ~10 requests (6 s apart, ~1 minute) to the smartphones feed and prints what each
variant gets. Run:  python probe_paging.py   Output also goes to data/probe_paging.txt
"""
import json
import time
from pathlib import Path

import requests

SLUG = "smartphones"
FEED = "https://www.daraz.pk/{s}/?ajax=true&page={p}{extra}"
PAGE = "https://www.daraz.pk/{s}/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
GAP = 6
out_lines = []


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line)
    out_lines.append(line)


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def feed(s, page, extra=""):
    time.sleep(GAP)
    r = s.get(FEED.format(s=SLUG, p=page, extra=extra),
              headers={"Accept": "application/json, text/plain, */*", "Referer": PAGE.format(s=SLUG)},
              timeout=30)
    cache = {k: v for k, v in r.headers.items() if k.lower() in ("age", "x-cache", "cache-control", "eagleid", "via")}
    try:
        ids = [x["itemId"] for x in r.json()["mods"]["listItems"]]
    except Exception as e:
        say(f"    page {page}{extra}: HTTP {r.status_code}, not a feed ({e})")
        return []
    say(f"    page {page}{extra}: HTTP {r.status_code}, {len(ids)} items, first {ids[:2]} last {ids[-1:]}  {cache}")
    return ids


def compare(name, pages):
    p1 = set(pages[0])
    for n, ids in enumerate(pages[1:], start=2):
        overlap = len(p1 & set(ids))
        verdict = "REPEAT of page 1" if overlap >= 0.8 * max(len(ids), 1) else "new items (pages properly)"
        say(f"  -> {name}: page {n} shares {overlap}/{len(ids)} items with page 1 = {verdict}")


say("== A: no cookies, feed only (what the collector did)")
s = new_session()
a = [feed(s, 1, "&isFirstRequest=true"), feed(s, 2), feed(s, 3)]
compare("A", a)
say("   cookies after A:", sorted(s.cookies.keys()))

say("\n== B: fresh session, visit the category page first (like a browser), then the feed")
s = new_session()
time.sleep(GAP)
r = s.get(PAGE.format(s=SLUG), headers={"Accept": "text/html"}, timeout=30)
say(f"    category page: HTTP {r.status_code}, cookies now: {sorted(s.cookies.keys())}")
b = [feed(s, 1, "&isFirstRequest=true"), feed(s, 2), feed(s, 3)]
compare("B", b)

say("\n== C: same session as B, pages 2-3 again with isFirstRequest=true")
c = [b[0], feed(s, 2, "&isFirstRequest=true"), feed(s, 3, "&isFirstRequest=true")]
compare("C", c)

Path("data").mkdir(exist_ok=True)
Path("data/probe_paging.txt").write_text("\n".join(out_lines), encoding="utf-8")
say("\nsaved to data/probe_paging.txt")
