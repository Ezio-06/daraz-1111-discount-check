"""Builds the README charts (light + dark SVG) from docs/early_numbers.json.

Run: python tools/early_numbers.py, then python tools/make_readme_charts.py  (needs matplotlib;
the collector itself does not).
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "charts"
OUT.mkdir(parents=True, exist_ok=True)
NUM = json.loads((ROOT / "docs" / "early_numbers.json").read_text(encoding="utf-8"))

FONT = "Inter" if any("Inter" in f.name for f in font_manager.fontManager.ttflist) else "DejaVu Sans"
plt.rcParams.update({"font.family": FONT, "svg.fonttype": "path", "axes.unicode_minus": False,
                     "svg.hashsalt": "daraz"})

# Surfaces match GitHub's light / dark backgrounds; categorical slots validated (CVD, contrast) for both.
THEMES = {
    "light": dict(bg="#ffffff", ink="#1f2328", ink2="#59636e", muted="#818b98", grid="#e6e8eb",
                  base="#c3c8cf", track="#eef0f2",
                  group={"tight-margin": "#2a78d6", "discount-heavy": "#eb6834", "control": "#1baf7a"}),
    "dark": dict(bg="#0d1117", ink="#f0f6fc", ink2="#c3c9d1", muted="#8b949e", grid="#21262d",
                 base="#3d444d", track="#1c2128",
                 group={"tight-margin": "#3987e5", "discount-heavy": "#d95926", "control": "#199e70"}),
}
GROUP_NAME = {"tight-margin": "Tight margins", "discount-heavy": "Discount-heavy", "control": "Control"}
when = NUM["started_at"][:10]


def frame(t, title, subtitle, source, h):
    fig, ax = plt.subplots(figsize=(9.0, h), dpi=100)
    fig.patch.set_facecolor(t["bg"])
    ax.set_facecolor(t["bg"])
    fig.subplots_adjust(left=0.25, right=0.86, top=1 - 1.15 / h, bottom=0.55 / h)
    fig.text(0.02, 1 - 0.26 / h, title, fontsize=15, fontweight="bold", color=t["ink"], ha="left", va="top")
    fig.text(0.02, 1 - 0.62 / h, subtitle, fontsize=10.5, color=t["ink2"], ha="left", va="top")
    fig.text(0.02, 0.12 / h, source, fontsize=8.5, color=t["muted"], ha="left", va="bottom")
    for s in ("top", "right", "left", "bottom"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors=t["ink2"], labelsize=10, length=0)
    ax.grid(axis="x", color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def legend(fig, t, h):
    x = 0.25
    for g in ("tight-margin", "discount-heavy", "control"):
        fig.patches.append(matplotlib.patches.FancyBboxPatch(
            (x, 1 - 0.98 / h), 0.012, 0.012 * 9 / h, boxstyle="round,pad=0,rounding_size=0.002",
            transform=fig.transFigure, color=t["group"][g]))
        fig.text(x + 0.018, 1 - 0.98 / h + 0.006 * 9 / h, GROUP_NAME[g], fontsize=9.5, color=t["ink2"],
                 va="center")
        x += 0.17


def discounts(t, theme):
    cats = sorted(NUM["categories"].values(), key=lambda c: c["median_discount"])
    h = 0.42 * len(cats) + 1.9
    fig, ax = frame(t, "Over three weeks before 11.11, the 'sale' is already on",
                    "Median discount Daraz shows on listings with a crossed-out price. Right: share of listings showing one.",
                    f"Source: sweep {NUM['run_id']} ({when}) · ~10,300 listings · docs/early_numbers.json · "
                    "a shown discount is a claim, not proof of a price cut", h)
    legend(fig, t, h)
    ys = range(len(cats))
    vals = [c["median_discount"] for c in cats]
    ax.barh(ys, vals, height=0.62, color=[t["group"][c["group"]] for c in cats], zorder=3)
    for y, c in zip(ys, cats):
        ax.text(c["median_discount"] + 0.8, y, f"{c['median_discount']:g}%", va="center", fontsize=10,
                fontweight="bold", color=t["ink"])
        ax.text(1.02, y, f"{c['share_discounted']:.0f}%", transform=ax.get_yaxis_transform(), va="center",
                ha="left", fontsize=10, color=t["ink2"])
    ax.text(1.02, len(cats) - 0.35, "listings\ndiscounted", transform=ax.get_yaxis_transform(), va="bottom",
            ha="left", fontsize=8.5, color=t["muted"])
    ax.set_yticks(list(ys), [c["label"] for c in cats])
    ax.set_xlim(0, 52)
    ax.set_xticks([0, 10, 20, 30, 40, 50], ["0%", "10%", "20%", "30%", "40%", "50%"])
    fig.savefig(OUT / f"early_discounts-{theme}.svg", facecolor=fig.get_facecolor(), metadata={"Date": None})
    plt.close(fig)


def coverage(t, theme):
    cats = sorted(NUM["categories"].values(), key=lambda c: c["seen"] / c["in_bands"])
    h = 0.42 * len(cats) + 1.9
    fig, ax = frame(t, "How much of each category one sweep sees",
                    "Listings collected per sweep vs listings Daraz says sit in the same price bands.",
                    f"Source: sweep {NUM['run_id']} ({when}) · 30 bands x up to 40 listings · "
                    "8,190 is Daraz's display cap: those categories are bigger", h)
    ys = range(len(cats))
    ax.barh(ys, [100] * len(cats), height=0.62, color=t["track"], zorder=2)
    share = [100 * c["seen"] / c["in_bands"] for c in cats]
    ax.barh(ys, share, height=0.62, color=[t["group"][c["group"]] for c in cats], zorder=3)
    legend(fig, t, h)
    for y, c, s in zip(ys, cats, share):
        ax.text(1.02, y, f"{c['seen']:,} of {c['in_bands']:,}", transform=ax.get_yaxis_transform(),
                va="center", ha="left", fontsize=9.5, color=t["ink2"])
        ax.text(s - 1.2 if s > 20 else s + 1.2, y, f"{s:.0f}%", va="center", ha="right" if s > 20 else "left",
                fontsize=10, fontweight="bold", color="#ffffff" if s > 20 else t["ink"], zorder=4)
    ax.set_yticks(list(ys), [c["label"] for c in cats])
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    fig.savefig(OUT / f"coverage-{theme}.svg", facecolor=fig.get_facecolor(), metadata={"Date": None})
    plt.close(fig)


if __name__ == "__main__":
    for theme, t in THEMES.items():
        discounts(t, theme)
        coverage(t, theme)
    print("charts written to", OUT)
