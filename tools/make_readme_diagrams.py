"""Builds the README banner and diagrams (light + dark SVG). Run: python tools/make_readme_diagrams.py"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

OUT = Path(__file__).resolve().parent.parent / "assets" / "diagrams"
OUT.mkdir(parents=True, exist_ok=True)
FONT = "Inter" if any("Inter" in f.name for f in font_manager.fontManager.ttflist) else "DejaVu Sans"
plt.rcParams.update({"font.family": FONT, "svg.fonttype": "path", "svg.hashsalt": "daraz"})

THEMES = {
    "light": dict(bg="#ffffff", ink="#1f2328", ink2="#59636e", muted="#818b98", line="#c3c8cf",
                  box="#f6f8fa", accent="#eb6834", blue="#2a78d6", bad="#e34948", good="#1baf7a"),
    "dark": dict(bg="#0d1117", ink="#f0f6fc", ink2="#c3c9d1", muted="#8b949e", line="#3d444d",
                 box="#161b22", accent="#d95926", blue="#3987e5", bad="#e66767", good="#199e70"),
}


def canvas(t, w, h):
    fig = plt.figure(figsize=(w, h), dpi=100)
    fig.patch.set_facecolor(t["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.axis("off")
    return fig, ax


def box(ax, t, x, y, w, h, title, body="", edge=None):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.08",
                                facecolor=t["box"], edgecolor=edge or t["line"], linewidth=1.2))
    ax.text(x + w / 2, y + h - 0.2, title, ha="center", va="top", fontsize=10.5, fontweight="bold", color=t["ink"])
    if body:
        ax.text(x + w / 2, y + h - 0.5, body, ha="center", va="top", fontsize=8.8, color=t["ink2"], linespacing=1.35)


def arrow(ax, t, a, b, color=None, style="-|>", rad=0.0, ls="-"):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=12, linewidth=1.4, linestyle=ls,
                                 color=color or t["muted"], connectionstyle=f"arc3,rad={rad}"))


def save(fig, name, theme):
    fig.savefig(OUT / f"{name}-{theme}.svg", facecolor=fig.get_facecolor(), metadata={"Date": None})
    plt.close(fig)


def banner(t, theme):
    fig, ax = canvas(t, 9, 2.0)
    ax.add_patch(FancyBboxPatch((0.05, 0.05), 8.9, 1.9, boxstyle="round,pad=0,rounding_size=0.12",
                                facecolor=t["box"], edgecolor=t["line"], linewidth=1))
    ax.text(0.4, 1.45, "Are Daraz's 11.11 discounts real?", fontsize=21, fontweight="bold", color=t["ink"], va="center")
    ax.text(0.4, 0.88, "~10,300 listings in 10 categories, every 6 hours,\nfrom mid-October until after the sale.",
            fontsize=11, color=t["ink2"], va="center")
    ax.text(0.4, 0.38, "If prices went up before they came 'down', the data will show it.",
            fontsize=11, color=t["ink2"], va="center")
    # a tiny price line: flat, raised before the sale, "discounted" back to the old price
    xs = [6.6, 7.2, 7.45, 7.9, 8.1, 8.6]
    ys = [0.95, 0.95, 1.45, 1.45, 0.95, 0.95]
    ax.plot(xs, ys, color=t["accent"], linewidth=2.4, solid_capstyle="round")
    ax.plot([6.6, 8.6], [0.95, 0.95], color=t["line"], linewidth=1, linestyle=(0, (3, 3)), zorder=0)
    ax.text(7.67, 1.58, "raised", ha="center", fontsize=8.5, color=t["ink2"])
    ax.text(8.35, 0.72, "\"-33%\"", ha="center", fontsize=8.5, color=t["accent"], fontweight="bold")
    ax.text(6.6, 0.72, "price", ha="left", fontsize=8.5, color=t["muted"])
    save(fig, "banner", theme)


def bands(t, theme):
    fig, ax = canvas(t, 9, 3.3)
    ax.text(0.3, 3.05, "Problem: the feed ignores page numbers", fontsize=11.5, fontweight="bold", color=t["ink"], va="top")
    for i, (lbl, ok) in enumerate([("page 1", True), ("page 2", False), ("page 3", False)]):
        x = 0.3 + i * 1.35
        box(ax, t, x, 1.75, 1.15, 0.9, lbl, "40 listings" if ok else "= page 1 again", edge=None if ok else t["bad"])
    ax.text(0.3, 1.45, "Cookies and cache-busters don't help\n(probe_paging.py, 2026-10-17).", fontsize=8.8,
            color=t["ink2"], va="top", linespacing=1.35)

    ax.text(4.6, 3.05, "Fix: cut the category by price, read page 1 of each slice", fontsize=11.5,
            fontweight="bold", color=t["ink"], va="top")
    edges = ["Rs 1", "", "", "", "", "", "", "Rs 20M"]
    widths = [0.35, 0.4, 0.45, 0.5, 0.55, 0.65, 1.3]
    x = 4.6
    for i, w in enumerate(widths):
        ax.add_patch(FancyBboxPatch((x, 1.95), w - 0.05, 0.6, boxstyle="round,pad=0,rounding_size=0.05",
                                    facecolor=t["blue"] if i < 6 else t["accent"], edgecolor="none", alpha=0.9))
        ax.text(x + (w - 0.05) / 2, 2.25, "≤40" if i < 6 else "top 40", ha="center", va="center",
                fontsize=8, color="#ffffff", fontweight="bold")
        x += w
    ax.text(4.6, 1.8, "Rs 1", fontsize=8.5, color=t["muted"], va="top")
    ax.text(x - 0.05, 1.8, "Rs 20M", fontsize=8.5, color=t["muted"], va="top", ha="right")
    ax.text(4.6, 1.35, "• First sweep splits the price range in half (log scale) until every\n"
            "   slice holds ≤ 40 listings, or the category has 30 slices.\n"
            "• The slices are then frozen, so every sweep reads the same ones.\n"
            "• Slices cover Rs 1 to 20M with no gaps: a changed price\n   always lands in some slice.",
            fontsize=8.8, color=t["ink2"], va="top", linespacing=1.45)
    save(fig, "price_bands", theme)


def pipeline(t, theme):
    fig, ax = canvas(t, 9, 2.6)
    steps = [("Daraz feed", "page 1 of a band\n1 request / 6 s"),
             ("Raw answer", "saved gzipped\nbefore any parsing"),
             ("Checks", "price filter honoured?\nrepeated answer?"),
             ("SQLite", "runs · bands ·\nproducts · snapshots")]
    xs = [0.3, 2.55, 4.8, 7.05]
    for (title, body), x in zip(steps, xs):
        box(ax, t, x, 1.0, 1.65, 1.15, title, body)
    for a, b in zip(xs, xs[1:]):
        arrow(ax, t, (a + 1.7, 1.58), (b - 0.05, 1.58))
    arrow(ax, t, (3.38, 0.98), (7.88, 0.98), color=t["accent"], rad=0.25, ls=(0, (4, 3)))
    ax.text(5.63, 0.28, "--reparse: rebuild any sweep from raw files after a parser fix (nothing re-downloaded)",
            ha="center", fontsize=8.8, color=t["accent"])
    ax.text(5.63, 2.45, "fails 3 times in a row → stop the sweep, back off 1 h, 2 h, 4 h … 12 h",
            ha="center", fontsize=8.8, color=t["ink2"])
    save(fig, "pipeline", theme)


if __name__ == "__main__":
    for theme, t in THEMES.items():
        banner(t, theme)
        bands(t, theme)
        pipeline(t, theme)
    print("diagrams written to", OUT)
