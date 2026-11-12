# Changes

## 1.0.0 (12 November 2026)

First public version.

- One `master.tex` gives the working paper, the anonymous copy, the title page and the online appendix. The `mode` setting chooses which one is built. `build.py build split` writes the three files that journal sites ask for.
- The front matter uses the standard LaTeX commands (`\title`, `\subtitle`, `\author[n,*]`, `\affil`, `\email`, `\orcid`, `\thanks`, `\maketitle` and the `abstract` environment). The style arranges one to six authors.
- Co-authors can leave notes in the margin with `\mnote`, `\discuss` and `\replace`. `notes = hide` takes them out without moving a line of the text.
- Sections and blocks of text are switched on and off with `\ecofintoggle`, `\ecofininput` and `ecofinblock`.
- Headings are set in Geist Serif, the text in Source Serif Pro and the mathematics in Latin Modern. STIX Two and Libertinus are options for the mathematics.
- Captions stand above tables and figures, with their notes below. The online appendix has its own reference list.
- `build.py` builds, checks and packs the paper. `figstyle_ecofin.py` gives Matplotlib figures the same colours and widths.
