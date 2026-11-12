#!/usr/bin/env python3
"""
figstyle_ecofin.py: Matplotlib settings that match ecofin.sty.

Copyright (c) 2026 Fatih Kansoy. MIT License.

- Width: figures are drawn at 0.94 of the text width (6.11 in on US Letter, 5.89 in on
  A4) and included at 0.94\\textwidth, so type in the figure prints at its true size.
- Fonts are stored as TrueType (type 42), never as bitmaps.
- Each series has a colour, a marker and a line style, so groups can be told apart in
  black and white. The colours are those of ecofin.sty (Okabe and Ito; Paul Tol).
- No title and no notes inside the image: the caption goes above the figure in LaTeX and
  the notes below it, in the figurenotes environment.

    python3 figstyle_ecofin.py --demo out     draws an example into out/
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import to_hex, to_rgb
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# ------------------------------------------------------------------ dimensions
# US Letter (8.5in - 2*1in = 6.5in = 469.755 TeX pt; 0.94 * 6.5in = 6.110in)
# A4 Paper  (210mm - 2*1in = 6.268in = 452.968 TeX pt; 0.94 * 6.268in = 5.892in)
TEXTWIDTH_PT = 469.755
TEXTWIDTH_A4_PT = 452.968
TEX_PT_PER_IN = 72.27
SCALE = 0.94


def figure_width(textwidth_pt: float = TEXTWIDTH_PT, fraction: float = SCALE) -> float:
    """Convert LaTeX \\textwidth (in TeX points, 72.27 pt/in) to exact inches."""
    return fraction * (textwidth_pt / TEX_PT_PER_IN)


W = figure_width(TEXTWIDTH_PT, SCALE)         # 6.110 in (letterpaper default)
W_A4 = figure_width(TEXTWIDTH_A4_PT, SCALE)   # 5.892 in (a4paper option)
ROW = 1.95
FOOT = 0.32
TOP = 0.28

SIZE = {"base": 7.5, "title": 8.0, "label": 7.5, "tick": 7.0, "legend": 7.0, "small": 6.5}

# ------------------------------------------------------------------ palette
PALETTE = {
    "primary": dict(color="#0072B2", marker="o", ls="-"),
    "secondary": dict(color="#D55E00", marker="s", ls="--"),
    "tertiary": dict(color="#009E73", marker="^", ls="-."),
    "quaternary": dict(color="#CC79A7", marker="X", ls=(0, (3, 1, 1, 1, 1, 1))),
    "reference": dict(color="#000000", marker="D", ls="-"),
    "wine": dict(color="#882255", marker="v", ls=(0, (6, 1.5)), hollow=True),
    "sky": dict(color="#56B4E9", marker="P", ls=(0, (1, 1)), hollow=True),
    "brickred": dict(color="#CC4054", marker="o", ls="-"),
}

GREY_STRATA = [
    dict(fill="#3A3A3A", color="#3A3A3A", marker="o", ls="-"),
    dict(fill="#7A7A7A", color="#7A7A7A", marker="s", ls="--"),
    dict(fill="#BDBDBD", color="#9A9A9A", marker="^", ls=(0, (1, 1))),
    dict(fill="white", hatch="//////", edge="#5A5A5A", color="#5A5A5A", marker="D", ls="-."),
    dict(fill="white", hatch="....", edge="#5A5A5A", color="#5A5A5A", marker="v", ls=":"),
]

SHADE = "0.9"
LW = 1.2
MS = 3.2
MS_EST = 4.5
ALPHA_BAND = 0.14


def register_fonts(font_dir: str | Path) -> list[str]:
    """Register all .otf/.ttf font files in `font_dir` with Matplotlib."""
    d = Path(font_dir)
    names: list[str] = []
    if not d.is_dir():
        return names
    for p in sorted(d.glob("*.[ot]tf")):
        font_manager.fontManager.addfont(str(p))
        names.append(font_manager.FontProperties(fname=str(p)).get_name())
    return sorted(set(names))


def rc_params(serif_fonts: list[str] | None = None) -> dict:
    """Matplotlib settings as a dictionary; nothing global is changed."""
    return {
        "font.family": "serif",
        "font.serif": serif_fonts or ["Source Serif Pro", "Source Serif 4", "DejaVu Serif"],
        "font.size": SIZE["base"],
        "axes.titlesize": SIZE["title"],
        "axes.labelsize": SIZE["label"],
        "xtick.labelsize": SIZE["tick"],
        "ytick.labelsize": SIZE["tick"],
        "legend.fontsize": SIZE["legend"],
        "axes.titlelocation": "left",
        "axes.titlepad": 4.0,
        "axes.linewidth": 0.6,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.unicode_minus": True,
        "xtick.major.width": 0.6,
        "ytick.major.width": 0.6,
        "xtick.major.size": 2.5,
        "ytick.major.size": 2.5,
        "lines.linewidth": LW,
        "lines.markersize": MS,
        "lines.markeredgewidth": 0.8,
        "errorbar.capsize": 0,
        "legend.frameon": False,
        "legend.handlelength": 2.6,
        "legend.columnspacing": 1.4,
        "hatch.linewidth": 0.5,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "savefig.dpi": 300,
        "figure.dpi": 100,
        "axes.axisbelow": True,
    }


@contextmanager
def style_context(serif_fonts: list[str] | None = None):
    """Apply the settings inside a with-block only."""
    with matplotlib.rc_context(rc_params(serif_fonts)):
        yield


def setup(serif_fonts: list[str] | None = None):
    """Apply the settings for the whole script."""
    plt.rcParams.update(rc_params(serif_fonts))
    return plt


def figure(
    nrows: int = 1,
    ncols: int = 1,
    height: float | None = None,
    width: float = W,
    apply_rc: bool = True,
    **kw,
):
    h = height if height is not None else nrows * ROW + FOOT
    if apply_rc:
        with style_context():
            return plt.subplots(nrows, ncols, figsize=(width, h), **kw)
    return plt.subplots(nrows, ncols, figsize=(width, h), **kw)


def panel(ax, text: str, pad: float = 4.0, size: float | None = None):
    ax.set_title(text, loc="left", fontsize=size or SIZE["title"], pad=pad)


def reference_line(ax, at: float = 0.0, vertical: bool = False):
    kw = dict(color="black", lw=0.8, zorder=1.5)
    return (ax.axvline if vertical else ax.axhline)(at, **kw)


def shade_window(ax, x0: float, x1: float):
    return ax.axvspan(x0, x1, color=SHADE, zorder=0, lw=0)


def sty(
    slot: str = "primary",
    label: str | None = None,
    hollow: bool | None = None,
    line: bool = True,
    **over,
) -> dict:
    s = PALETTE[slot].copy()
    h = s.pop("hollow", False) if hollow is None else hollow
    out = dict(
        color=s["color"],
        marker=s["marker"],
        ls=s["ls"] if line else "none",
        mfc="white" if h else s["color"],
        mec=s["color"],
    )
    if label is not None:
        out["label"] = label
    out.update(over)
    return out


def band(ax, x, lo, hi, slot: str = "primary"):
    return ax.fill_between(
        x, lo, hi, color=PALETTE[slot]["color"], alpha=ALPHA_BAND, lw=0, zorder=1
    )


def estimate(
    ax,
    x,
    y,
    lo=None,
    hi=None,
    slot: str = "primary",
    label: str | None = None,
    horizontal: bool = False,
    hollow: bool | None = None,
    ms: float = MS_EST,
    **over,
):
    s = sty(slot, label=label, hollow=hollow, line=False)
    s.update(over)
    v = np.atleast_1d(np.asarray(x if horizontal else y, dtype=float))
    err = (
        None
        if lo is None
        else np.vstack([
            v - np.atleast_1d(np.asarray(lo, dtype=float)),
            np.atleast_1d(np.asarray(hi, dtype=float)) - v,
        ])
    )
    kw = dict(
        fmt=s.pop("marker"),
        color=s.pop("color"),
        ms=ms,
        elinewidth=0.9,
        capsize=0,
        zorder=3,
    )
    s.pop("ls", None)
    kw.update(s)
    return ax.errorbar(x, y, xerr=err, **kw) if horizontal else ax.errorbar(x, y, yerr=err, **kw)


def ramp(slot: str = "primary", n: int = 3) -> list[str]:
    base_hex = "#6A6A6A" if slot == "reference" else PALETTE[slot]["color"]
    c = np.array(to_rgb(base_hex))
    return [
        to_hex(c * (1 + f) if f < 0 else c + f * (1 - c))
        for f in np.linspace(-0.45, 0.6, n)
    ]


def legend(fig, handles, ncol: int | None = None, y: float = 0.0, **kw):
    k = dict(
        loc="lower center",
        ncol=ncol or min(len(handles), 5),
        frameon=False,
        fontsize=SIZE["legend"],
        bbox_to_anchor=(0.5, y),
        handlelength=2.6,
    )
    k.update(kw)
    return fig.legend(handles=handles, **k)


def finish(fig, foot: float = FOOT, top: float = 0.0, **kw):
    fig.tight_layout(rect=(0, foot / fig.get_figheight(), 1, 1), **kw)
    if top:
        fig.subplots_adjust(top=fig.subplotpars.top - top / fig.get_figheight())


def save(fig, outdir: str | Path, stem: str, png: bool = True):
    d = Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        d / f"{stem}.pdf",
        metadata={
            "CreationDate": None,
            "ModDate": None,
            "Producer": None,
            "Creator": "figstyle_ecofin.py",
        },
    )
    if png:
        fig.savefig(d / f"{stem}.png", dpi=300, metadata={"Software": None})
    plt.close(fig)


def render_demo(outdir: str | Path) -> Path:
    """Draw a two-panel example as PDF and PNG."""
    with style_context():
        fig, axes = plt.subplots(1, 2, figsize=(W, 2.35))
        k = np.arange(-5, 9)
        b = np.array([1.1, -0.7, 0.4, -0.8, 0.0, -9.8, -14.6, -17.3, -19.0, -20.1, -20.8, -21.2, -21.3, -21.5])
        se = np.array([2.3, 2.1, 1.9, 1.6, 0.0, 2.0, 2.3, 2.5, 2.7, 2.9, 3.1, 3.3, 3.6, 3.9])

        ax = axes[0]
        panel(ax, "(a) Event study around the break")
        shade_window(ax, -0.5, 8.6)
        reference_line(ax, 0.0)
        ax.axvline(-0.5, color="black", lw=0.6, ls=(0, (3, 2)), zorder=1)
        band(ax, k, b - 1.96 * se, b + 1.96 * se, "primary")
        (h1,) = ax.plot(k, b, **sty("primary", label="Estimate, 95% interval"))
        (h2,) = ax.plot([-1], [0], ls="none", marker="o", ms=4.5, mfc="white", mec=h1.get_color(), mew=1.0, label="Reference year")
        ax.set_xlim(-5.6, 8.6)
        ax.set_xticks(range(-4, 9, 2))
        ax.set_xlabel("Years relative to the break")
        ax.set_ylabel("Per cent, relative to year \u22121")

        ax2 = axes[1]
        panel(ax2, "(b) Specification stability (post-break)")
        specs = np.arange(4)
        coefs = np.array([-18.4, -17.8, -16.3, -19.1])
        ses = np.array([2.9, 3.1, 3.3, 3.4])
        reference_line(ax2, 0.0, vertical=True)
        estimate(
            ax2,
            coefs,
            specs,
            lo=coefs - 1.96 * ses,
            hi=coefs + 1.96 * ses,
            slot="primary",
            horizontal=True,
        )
        ax2.set_yticks(specs)
        ax2.set_yticklabels(["Baseline", "Core rosters", "Tenure x year", "Cell FE"])
        ax2.invert_yaxis()
        ax2.set_xlabel("Estimated effect (%)")

        legend(fig, [h1, h2], ncol=2, y=0.01)
        finish(fig, foot=0.34)
        save(fig, outdir, "fig_demo_ecofin", png=True)
    return Path(outdir) / "fig_demo_ecofin.pdf"


if __name__ == "__main__":
    matplotlib.use("Agg")
    ap = argparse.ArgumentParser(description="Matplotlib settings that match ecofin.sty")
    ap.add_argument("--demo", metavar="OUTDIR", help="draw an example into OUTDIR")
    args = ap.parse_args()
    if args.demo:
        out = render_demo(args.demo)
        print(f"Wrote demo figure: {out}")
