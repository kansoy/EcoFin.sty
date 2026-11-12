#!/usr/bin/env python3
"""
build.py: builds, checks and packs a paper written with ecofin.sty.

Copyright (c) 2026 Fatih Kansoy. MIT License.

  python3 build.py doctor                      check that the TeX programs and fonts are there
  python3 build.py init <folder>               start a new paper from sample/
  python3 build.py build wp [--mode MODE]      build one PDF (wp, blind, article, blindarticle,
                                               titlepage, oa, blindoa)
  python3 build.py build split                 build the three files journal sites ask for:
                                               title page, anonymous manuscript, online appendix
  python3 build.py check <file.tex>            check structure, log and, with --anonymous, names
  python3 build.py bundle overleaf|lattice|arxiv   pack a starter or upload file
  python3 build.py export-public-repo <folder> copy the public files into a clean folder
  python3 build.py test-all                    run every test (about four minutes)

Options: --source, --out, --engine lualatex|xelatex|pdflatex, --verify.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

SUITE_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = SUITE_DIR / "sample"
FONTS_DIR = SUITE_DIR / "fonts"

ENGINES = ("lualatex", "xelatex", "pdflatex")
MODES = ("wp", "blind", "article", "blindarticle", "titlepage", "oa", "blindoa")
POPPLER_TOOLS = ("pdffonts", "pdfinfo", "pdftotext")
FIXED_MTIME = 1762819200  # 2025-11-11T00:00:00Z deterministic archive timestamp

PUBLIC_ALLOWLIST = [
    "ecofin.sty",
    "bibliography.bst",
    "figstyle_ecofin.py",
    "build.py",
    "Makefile",
    "latexmkrc",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "CITATION.cff",
    "CHANGELOG.md",
    "README.md",
    ".gitignore",
    ".github/workflows/ci.yml",
    "docs/images/banner.png",
    "docs/images/page.png",
    "docs/images/versions.png",
    "docs/images/notes.png",
    "docs/images/authors.png",
    "docs/images/options.png",
    "docs/images/check.png",
    "fonts/GeistSerif-Regular.ttf",
    "fonts/GeistSerif-Variable.ttf",
    "fonts/geist-OFL.txt",
    "fonts/ORIGIN.json",
    "sample/master.tex",
    "sample/master.pdf",
    "sample/latexmkrc",
    "sample/references.bib",
    "sample/sections/01_introduction.tex",
    "sample/sections/02_data_and_strategy.tex",
    "sample/sections/10_online_appendix.tex",
    "sample/tables/table1_summary.tex",
    "sample/tables/tableA1_checks.tex",
]

FORBIDDEN_PUBLIC_PATTERNS = (
    (r"/Users/[A-Za-z0-9._-]+", "hardcoded macOS user path"),
    (r"/home/[A-Za-z0-9._-]+", "hardcoded Linux user path"),
    (r"C:\\\\Users\\\\[A-Za-z0-9._-]+", "hardcoded Windows user path"),
    (r"\bsk-[A-Za-z0-9\-]{8,}", "API secret prefix"),
    (r"FROZEN" + r"\.sha256", "internal freeze manifest reference"),
)


@dataclass
class Issue:
    severity: str  # ERROR or WARN
    code: str
    message: str


@dataclass
class CheckReport:
    target: str
    mode: str  # FULL_BUILT or STATIC_ONLY
    anonymous_mode: bool
    style_resolved: str | None
    files_scanned: int
    tables_checked: int
    figures_checked: int
    has_online_appendix: bool
    passed: bool
    issues: list[Issue] = field(default_factory=list)
    pdf_info: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers: archive paths, TeX comments, labels shared between split files
# ---------------------------------------------------------------------------

def strip_tex_comments(text: str) -> str:
    out_lines: list[str] = []
    for line in text.splitlines():
        m = re.search(r"(?<!\\)%", line)
        out_lines.append(line[: m.start()] if m else line)
    return "\n".join(out_lines)


def checked_members(root: Path, names: list[str]):
    """Validate archive member paths (no symlinks, traversal, or case collisions)."""
    root_resolved = root.resolve(strict=True)
    seen: set[str] = set()
    for name in sorted(names):
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or "\\" in name:
            raise ValueError(f"Unsafe archive member name: {name}")
        key = rel.as_posix().casefold()
        if key in seen:
            raise ValueError(f"Case-colliding archive member: {name}")
        seen.add(key)

        cur = root_resolved
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                raise ValueError(f"Symlink forbidden in archive: {name}")
        resolved = cur.resolve(strict=True)
        resolved.relative_to(root_resolved)
        if not resolved.is_file():
            raise ValueError(f"Missing regular file for archive: {name}")
        yield rel.as_posix(), resolved


def collect_aux_labels(aux_path: Path, visited: set[Path] | None = None) -> tuple[list[str], int]:
    """Collect the \\newlabel lines of an .aux file and its children, and the last equation number."""
    if visited is None:
        visited = set()
    aux_resolved = aux_path.resolve()
    if aux_resolved in visited or not aux_resolved.is_file():
        return [], 0
    visited.add(aux_resolved)

    lines: list[str] = []
    max_eq = 0
    raw = aux_resolved.read_text(encoding="utf-8", errors="ignore")
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith(r"\newlabel{"):
            lines.append(s)
            m_eq = re.match(r"\\newlabel\{eq:[^}]+\}\{\{(\d+)\}", s)
            if m_eq:
                max_eq = max(max_eq, int(m_eq.group(1)))
        elif s.startswith(r"\@input{") and s.endswith("}"):
            child_name = s[len(r"\@input{") : -1]
            child_lines, child_eq = collect_aux_labels(aux_resolved.parent / child_name, visited)
            lines.extend(child_lines)
            max_eq = max(max_eq, child_eq)
    return lines, max_eq


def write_xref_bundle(aux_path: Path, xref_dir: Path, stem: str) -> str:
    """Write `<stem>.xref.aux` (labels only) and `<stem>.eq.tex`; return their SHA-256."""
    xref_dir.mkdir(parents=True, exist_ok=True)
    labels, max_eq = collect_aux_labels(aux_path)
    xref_aux = xref_dir / f"{stem}.xref.aux"
    body = "% Sanitized label-only cross-reference file (ecofin build.py)\n\\relax\n"
    if labels:
        body += "\n".join(labels) + "\n"
    xref_aux.write_text(body, encoding="utf-8")

    eq_tex = xref_dir / f"{stem}.eq.tex"
    eq_tex.write_text(
        f"% Final main-text equation counter for continuous split Online Appendix numbering\n"
        f"\\ifdefined\\theHequation\\setcounter{{equation}}{{{max_eq}}}\\fi\n",
        encoding="utf-8",
    )
    return hashlib.sha256((body + f"|eq={max_eq}").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Building (each build runs in its own copy of the paper)
# ---------------------------------------------------------------------------

def copy_project_closure(
    src_dir: Path,
    stage_dir: Path,
    exclude_private: bool = False,
    include_fonts: bool = True,
) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    skip_dirs = {"build", "dist", "_build", ".git", "__pycache__"}
    skip_exts = {".aux", ".bbl", ".blg", ".fdb_latexmk", ".fls", ".log", ".out", ".toc", ".gz"}
    for root_str, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith("_stage_")]
        rel_root = Path(root_str).relative_to(src_dir)
        for fname in files:
            if any(fname.endswith(ext) for ext in skip_exts):
                continue
            if fname in {"master.pdf", "main.pdf", "1_title_page.pdf", "2_manuscript_anonymous.pdf", "3_online_appendix.pdf"}:
                continue
            rel_file = rel_root / fname
            if exclude_private and rel_file.as_posix() == "frontmatter/private.tex":
                continue
            dst = stage_dir / rel_file
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root_str) / fname, dst)

    for support in ("ecofin.sty", "bibliography.bst", "latexmkrc"):
        src_sup = SUITE_DIR / support
        dst_sup = stage_dir / support
        if src_sup.is_file() and not dst_sup.exists():
            shutil.copy2(src_sup, dst_sup)

    if include_fonts and FONTS_DIR.is_dir() and not (stage_dir / "fonts").exists():
        shutil.copytree(FONTS_DIR, stage_dir / "fonts")


def compile_tex_in_stage(
    stage_dir: Path,
    tex_name: str,
    engine: str = "lualatex",
    run_bibtex: bool = True,
    jobname: str | None = None,
    build_mode: str | None = None,
) -> None:
    if engine not in ENGINES:
        raise ValueError(f"Unsupported engine `{engine}`; choose from {ENGINES}")
    if shutil.which(engine) is None:
        raise RuntimeError(f"TeX engine `{engine}` not found on PATH")

    stem = jobname or Path(tex_name).stem
    base_cmd = [
        engine,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-recorder",
    ]
    if jobname:
        base_cmd.append(f"-jobname={jobname}")
    if build_mode:
        base_cmd.append(rf"\def\EcofinBuildMode{{{build_mode}}}\input{{{tex_name}}}")
    else:
        base_cmd.append(tex_name)

    def _run(cmd: list[str]) -> None:
        proc = subprocess.run(cmd, cwd=stage_dir, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            tail = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-35:])
            raise RuntimeError(f"Command failed ({' '.join(cmd)}) in {stage_dir}:\n{tail}")

    _run(base_cmd)

    if run_bibtex and shutil.which("bibtex"):
        main_aux = stage_dir / f"{stem}.aux"
        if main_aux.is_file() and r"\bibdata{" in main_aux.read_text(encoding="utf-8", errors="ignore"):
            _run(["bibtex", stem])
        for bu_aux in sorted(stage_dir.glob("bu*.aux")):
            if r"\bibdata{" in bu_aux.read_text(encoding="utf-8", errors="ignore"):
                _run(["bibtex", bu_aux.stem])

    _run(base_cmd)
    _run(base_cmd)


def resolve_entry_tex(stage_dir: Path, entry_tex: str = "master.tex") -> str:
    for cand in (entry_tex, "main.tex", "master_template.tex"):
        if (stage_dir / cand).is_file():
            return cand
    raise FileNotFoundError(f"No root .tex file found in {stage_dir} (checked {entry_tex}, main.tex)")


def build_wp(
    src_dir: Path,
    out_dir: Path,
    engine: str = "lualatex",
    include_fonts: bool = True,
    entry_tex: str = "master.tex",
    mode: str | None = None,
) -> Path:
    is_blind = mode in ("blind", "blindarticle", "blindoa")
    copy_project_closure(src_dir, out_dir, exclude_private=is_blind, include_fonts=include_fonts)
    entry_tex = resolve_entry_tex(out_dir, entry_tex)

    master_clean = strip_tex_comments((out_dir / entry_tex).read_text(encoding="utf-8", errors="ignore"))
    oa_hidden = mode in ("article", "blindarticle") or (
        mode is None and bool(re.search(r"showoa\s*=\s*false", master_clean))
    )
    oa_present = any(
        "ecofinonlineappendix" in strip_tex_comments(fp.read_text(encoding="utf-8", errors="ignore"))
        for fp in resolve_input_graph(out_dir / entry_tex)
    )
    xref_oa = out_dir / "xref" / "3_online_appendix.xref.aux"
    if oa_hidden and oa_present and not xref_oa.is_file():
        with tempfile.TemporaryDirectory(prefix="ecofin_oaxref_") as tmp_oa:
            st_main = Path(tmp_oa) / "main"
            st_oa = Path(tmp_oa) / "oa"
            copy_project_closure(src_dir, st_main, exclude_private=True, include_fonts=include_fonts)
            compile_tex_in_stage(st_main, entry_tex, engine=engine, run_bibtex=False, build_mode="blindarticle")
            shared_xref = out_dir / "xref"
            write_xref_bundle(st_main / f"{Path(entry_tex).stem}.aux", shared_xref, "2_manuscript_anonymous")
            copy_project_closure(src_dir, st_oa, exclude_private=True, include_fonts=include_fonts)
            shutil.copytree(shared_xref, st_oa / "xref", dirs_exist_ok=True)
            compile_tex_in_stage(
                st_oa,
                entry_tex,
                engine=engine,
                run_bibtex=False,
                jobname="3_online_appendix",
                build_mode="blindoa",
            )
            write_xref_bundle(st_oa / "3_online_appendix.aux", shared_xref, "3_online_appendix")

    # A standalone online appendix refers back to tables, sections and equations of the
    # paper. Build the paper once, out of sight, and hand its labels to the appendix.
    xref_ms = out_dir / "xref" / "2_manuscript_anonymous.xref.aux"
    if mode in ("oa", "blindoa") and oa_present and not xref_ms.is_file():
        with tempfile.TemporaryDirectory(prefix="ecofin_msxref_") as tmp_ms:
            st_main = Path(tmp_ms) / "main"
            copy_project_closure(src_dir, st_main, exclude_private=True, include_fonts=include_fonts)
            compile_tex_in_stage(st_main, entry_tex, engine=engine, run_bibtex=False, build_mode="blindarticle")
            write_xref_bundle(st_main / f"{Path(entry_tex).stem}.aux", out_dir / "xref", "2_manuscript_anonymous")

    compile_tex_in_stage(out_dir, entry_tex, engine=engine, run_bibtex=True, build_mode=mode)
    return out_dir / f"{Path(entry_tex).stem}.pdf"


def build_split(
    src_dir: Path,
    out_dir: Path,
    engine_anon: str = "pdflatex",
    engine_title: str | None = None,
) -> dict[str, Path]:
    """
    Build the three files journal sites ask for: `1_title_page.pdf`, `2_manuscript_anonymous.pdf`
    and `3_online_appendix.pdf`, from one `master.tex` (the mode is passed in), or from three
    separate .tex files of those names if the folder has them.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    eng_title = engine_title or ("lualatex" if shutil.which("lualatex") else engine_anon)

    stage_title = out_dir / "_stage_title"
    stage_ms = out_dir / "_stage_ms"
    stage_oa = out_dir / "_stage_oa"
    shared_xref = out_dir / "xref"
    shared_xref.mkdir(parents=True, exist_ok=True)

    has_legacy_split = all(
        (src_dir / f"{stem}.tex").is_file()
        for stem in ("1_title_page", "2_manuscript_anonymous", "3_online_appendix")
    )

    # 1. Title page, with names
    copy_project_closure(src_dir, stage_title, exclude_private=False, include_fonts=True)
    if has_legacy_split:
        compile_tex_in_stage(stage_title, "1_title_page.tex", engine=eng_title, run_bibtex=False)
    else:
        root_tex = resolve_entry_tex(stage_title)
        compile_tex_in_stage(
            stage_title,
            root_tex,
            engine=eng_title,
            run_bibtex=False,
            jobname="1_title_page",
            build_mode="titlepage",
        )

    # 2. Anonymous manuscript and online appendix, each in its own folder
    copy_project_closure(src_dir, stage_ms, exclude_private=True, include_fonts=True)
    copy_project_closure(src_dir, stage_oa, exclude_private=True, include_fonts=True)
    ms_tex = "2_manuscript_anonymous.tex" if has_legacy_split else resolve_entry_tex(stage_ms)
    oa_tex = "3_online_appendix.tex" if has_legacy_split else resolve_entry_tex(stage_oa)

    prev_sig = ""
    for _round in range(4):
        for st in (stage_ms, stage_oa):
            st_xref = st / "xref"
            if st_xref.exists():
                shutil.rmtree(st_xref)
            shutil.copytree(shared_xref, st_xref)

        compile_tex_in_stage(
            stage_ms,
            ms_tex,
            engine=engine_anon,
            run_bibtex=True,
            jobname=None if has_legacy_split else "2_manuscript_anonymous",
            build_mode=None if has_legacy_split else "blindarticle",
        )
        h_ms = write_xref_bundle(stage_ms / "2_manuscript_anonymous.aux", shared_xref, "2_manuscript_anonymous")

        shutil.rmtree(stage_oa / "xref", ignore_errors=True)
        shutil.copytree(shared_xref, stage_oa / "xref")

        compile_tex_in_stage(
            stage_oa,
            oa_tex,
            engine=engine_anon,
            run_bibtex=True,
            jobname=None if has_legacy_split else "3_online_appendix",
            build_mode=None if has_legacy_split else "blindoa",
        )
        h_oa = write_xref_bundle(stage_oa / "3_online_appendix.aux", shared_xref, "3_online_appendix")

        cur_sig = f"{h_ms}:{h_oa}"
        if cur_sig == prev_sig and _round >= 1:
            break
        prev_sig = cur_sig

    copy_project_closure(src_dir, out_dir / "_clean_src", exclude_private=True, include_fonts=True)
    for item in (out_dir / "_clean_src").iterdir():
        dst = out_dir / item.name
        if not dst.exists():
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
    shutil.rmtree(out_dir / "_clean_src")

    outputs: dict[str, Path] = {}
    for stage, stem in (
        (stage_title, "1_title_page"),
        (stage_ms, "2_manuscript_anonymous"),
        (stage_oa, "3_online_appendix"),
    ):
        for ext in (".pdf", ".log", ".fls", ".aux", ".bbl", ".blg"):
            p = stage / f"{stem}{ext}"
            if p.is_file():
                shutil.copy2(p, out_dir / p.name)
        for bu in stage.glob("bu*.*"):
            shutil.copy2(bu, out_dir / f"{stem}_{bu.name}")
        outputs[stem] = out_dir / f"{stem}.pdf"

    for st in (stage_title, stage_ms, stage_oa):
        shutil.rmtree(st)

    return outputs


# ---------------------------------------------------------------------------
# Checks: structure, log, fonts, and names in anonymous files
# ---------------------------------------------------------------------------

def resolve_input_graph(master_tex: Path) -> list[Path]:
    visited: list[Path] = []
    seen: set[Path] = set()
    base_dir = master_tex.parent

    def _walk(p: Path) -> None:
        rp = p.resolve()
        if rp in seen or not rp.is_file():
            return
        seen.add(rp)
        visited.append(rp)
        clean = strip_tex_comments(rp.read_text(encoding="utf-8", errors="ignore"))
        for m in re.finditer(r"\\(?:ecofininput(?:\[[^\]]*\])?|input|include)\{([^}]+)\}", clean):
            rel = m.group(1).strip()
            cand = base_dir / (rel if rel.endswith(".tex") else f"{rel}.tex")
            if not cand.is_file():
                cand = rp.parent / (rel if rel.endswith(".tex") else f"{rel}.tex")
            if cand.is_file():
                _walk(cand)

    _walk(master_tex)
    return visited


def extract_private_leak_tokens(tex_path: Path) -> list[str]:
    """Collect author names, e-mail addresses and ORCIDs from \\author / \\email / \\orcid,
    \\ecofinauthor and an optional private.tex."""
    tex_dir = tex_path.parent
    tokens: list[str] = []

    scan_files = [tex_path]
    for cand in (tex_dir / "frontmatter" / "private.tex", tex_dir.parent / "frontmatter" / "private.tex"):
        if cand.is_file():
            scan_files.append(cand)

    for fp in scan_files:
        txt = strip_tex_comments(fp.read_text(encoding="utf-8", errors="ignore"))
        for m in re.finditer(r"mailto:([^}\s]+)", txt):
            tokens.append(m.group(1).lower())
        for m in re.finditer(r"\\ecofinauthor(?:\[[^\]]*\])?\{([^}]+)\}\{([^}]*)\}(?:\{([^}]*)\})?", txt, re.DOTALL):
            name = " ".join(m.group(1).split()).strip().lower()
            email = m.group(3).strip().lower() if m.group(3) else ""
            if len(name) >= 4:
                tokens.append(name)
            if email:
                tokens.append(email)
        for m in re.finditer(r"orcid\s*=\s*([0-9X\-]{15,25})", txt, re.IGNORECASE):
            tokens.append(m.group(1).strip().lower())
        # Standard front matter: \author[1,*]{Name}, \email{...}, \orcid{...}
        for m in re.finditer(r"\\author\s*\[[^\]]*\]\s*\{([^{}]+)\}", txt):
            name = " ".join(m.group(1).split()).strip().lower()
            if len(name) >= 4:
                tokens.append(name)
        for m in re.finditer(r"\\email\s*\{([^{}\s]+)\}", txt):
            tokens.append(m.group(1).strip().lower())
        for m in re.finditer(r"\\orcid\s*\{([0-9X\-]{15,25})\}", txt, re.IGNORECASE):
            tokens.append(m.group(1).strip().lower())
        for m in re.finditer(r"\\sc\s+([A-Za-zÀ-ÿ\-\s]+?)(?=\\thanks|\\and|\})", txt):
            name = " ".join(m.group(1).split()).strip().lower()
            if len(name) >= 4:
                tokens.append(name)
    return sorted(set(tokens))


def check_manuscript(
    tex_path: Path,
    anonymous: bool = False,
    require_built: bool = False,
    build_dir: Path | None = None,
    jobname: str | None = None,
) -> CheckReport:
    tex_path = tex_path.resolve()
    if not tex_path.is_file():
        raise FileNotFoundError(f"Manuscript .tex file not found: {tex_path}")

    bdir = (build_dir or tex_path.parent).resolve()
    stem = jobname or tex_path.stem
    issues: list[Issue] = []

    files = resolve_input_graph(tex_path)
    master_clean = strip_tex_comments(tex_path.read_text(encoding="utf-8", errors="ignore"))

    if re.search(r"anonymous\s*(?:=\s*true|\])|mode\s*=\s*blind", master_clean) or r"\usepackage[anonymous" in master_clean:
        anonymous = True

    # 1. Which style the paper loads
    style_resolved: str | None = None
    m_pkg = re.search(r"\\usepackage(?:\[[^\]]*\])?\{(ecofin|paper)\}", master_clean)
    if not m_pkg:
        issues.append(Issue("ERROR", "K_NO_STYLE", "Master .tex must load \\usepackage[...]{ecofin} (or legacy {paper})."))
    else:
        sname = f"{m_pkg.group(1)}.sty"
        for d in (tex_path.parent, bdir, SUITE_DIR):
            if (d / sname).is_file():
                style_resolved = str((d / sname).resolve())
                break

    if anonymous and re.search(r"\\input\{frontmatter/private(?:\.tex)?\}", master_clean):
        issues.append(Issue("ERROR", "K_ANON_PRIVATE_INPUT", "Anonymous build must NEVER \\input{frontmatter/private}."))

    # 2. Tables, figures and the online appendix in every included file
    tables_checked = 0
    figures_checked = 0
    has_oa = False

    for fp in files:
        rel = fp.name
        clean = strip_tex_comments(fp.read_text(encoding="utf-8", errors="ignore"))
        if any(tok in clean for tok in ("ecofinonlineappendix", r"\ecofinappendix", r"\appendix")):
            has_oa = True

        for env_name, body_pat, is_table in (
            ("table", r"\\begin\{(?:tabular|tabular\*|tabularx|threeparttable|adjustbox)", True),
            ("figure", r"\\includegraphics|\\begin\{tikzpicture\}", False),
        ):
            for m_env in re.finditer(rf"\\begin\{{{env_name}\*?\}}(.*?)\\end\{{{env_name}\*?\}}", clean, re.DOTALL):
                block = m_env.group(1)
                if is_table:
                    tables_checked += 1
                else:
                    figures_checked += 1
                m_cap = re.search(r"\\caption(?:\[[^\]]*\])?\{", block)
                m_body = re.search(body_pat, block)
                m_lbl = re.search(r"\\label\{([^}]+)\}", block)
                if m_cap and m_body and m_cap.start() > m_body.start():
                    issues.append(Issue("ERROR", f"K_{env_name.upper()}_CAPTION_BELOW", f"{rel}: \\caption must sit ABOVE {env_name} body."))
                if m_cap and m_lbl and m_lbl.start() < m_cap.start():
                    issues.append(Issue("ERROR", f"K_{env_name.upper()}_LABEL_BEFORE_CAPTION", f"{rel}: \\label{{{m_lbl.group(1)}}} appears before \\caption."))

    # 3. Built files (.log, .fls, .pdf)
    pdf_path = bdir / f"{stem}.pdf"
    log_path = bdir / f"{stem}.log"
    fls_path = bdir / f"{stem}.fls"
    pdf_info: dict[str, str] = {}
    mode = "STATIC_ONLY"

    if require_built:
        for req_ext, req_p in (("pdf", pdf_path), ("log", log_path), ("fls", fls_path)):
            if not req_p.is_file() or req_p.stat().st_size == 0:
                issues.append(Issue("ERROR", f"K_MISSING_{req_ext.upper()}", f"Required built artifact missing or empty: {req_p}"))
        for tool in POPPLER_TOOLS:
            if shutil.which(tool) is None:
                issues.append(Issue("ERROR", "K_MISSING_TOOL", f"Required PDF verifier `{tool}` not installed on PATH."))

    if log_path.is_file() and log_path.stat().st_size > 0:
        mode = "FULL_BUILT"
        log_txt = log_path.read_text(encoding="utf-8", errors="ignore")
        tex_errs = re.findall(r"^! (.+)$", log_txt, flags=re.MULTILINE)
        if tex_errs:
            issues.append(Issue("ERROR", "K_LOG_TEX_ERROR", f"{log_path.name}: {len(tex_errs)} TeX error(s): {tex_errs[:3]}"))
        undef_refs = re.findall(r"LaTeX Warning: Reference `([^']+)' on page \d+ undefined", log_txt)
        if undef_refs:
            issues.append(Issue("ERROR", "K_UNDEF_REF", f"{log_path.name}: Undefined reference(s): {sorted(set(undef_refs))[:5]}"))
        undef_cites = re.findall(r"Package natbib Warning: Citation `([^']+)' on page \d+ undefined", log_txt)
        if undef_cites:
            issues.append(Issue("ERROR", "K_UNDEF_CITE", f"{log_path.name}: Undefined citation(s): {sorted(set(undef_cites))[:5]}"))
        dup_dests = re.findall(r"pdfTeX warning \(ext4\): destination with the same identifier \(([^)]+)\)", log_txt)
        if dup_dests:
            issues.append(Issue("ERROR", "K_DUP_HYPERREF_DEST", f"{log_path.name}: {len(dup_dests)} duplicate hyperref destination(s): {dup_dests[:4]}"))
        overfulls = [float(x) for x in re.findall(r"Overfull \\hbox \(([0-9.]+)pt too wide\)", log_txt) if float(x) > 1.0]
        if overfulls:
            issues.append(Issue("WARN", "K_OVERFULL_HBOX", f"{log_path.name}: {len(overfulls)} overfull \\hbox (>1pt); max={max(overfulls):.2f}pt."))

    for blg in sorted(bdir.glob("*.blg")):
        btxt = blg.read_text(encoding="utf-8", errors="ignore")
        m_berr = re.search(r"There (?:was|were) (\d+) error message", btxt)
        if m_berr:
            issues.append(Issue("ERROR", "K_BIBTEX_ERR", f"{blg.name}: BibTeX reported {m_berr.group(1)} error(s)."))

    if fls_path.is_file() and anonymous:
        fls_txt = fls_path.read_text(encoding="utf-8", errors="ignore")
        if "frontmatter/private.tex" in fls_txt:
            issues.append(Issue("ERROR", "K_FLS_PRIVATE_LOADED", f"{fls_path.name} records INPUT of frontmatter/private.tex in anonymous mode."))

    if pdf_path.is_file() and pdf_path.stat().st_size > 0:
        mode = "FULL_BUILT"
        if shutil.which("pdfinfo"):
            res = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                issues.append(Issue("ERROR", "K_PDFINFO_FAIL", f"pdfinfo failed on {pdf_path.name}"))
            for line in res.stdout.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    pdf_info[k.strip()] = v.strip()

        if shutil.which("pdffonts"):
            res = subprocess.run(["pdffonts", str(pdf_path)], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                issues.append(Issue("ERROR", "K_PDFFONTS_FAIL", f"pdffonts failed on {pdf_path.name}"))
            for line in res.stdout.splitlines()[2:]:
                if "Type 3" in line:
                    issues.append(Issue("ERROR", "K_TYPE3_FONT", f"{pdf_path.name} embeds bitmap Type 3 font: {line.strip()}"))

        if anonymous:
            if pdf_info.get("Author", "").strip():
                issues.append(Issue("ERROR", "K_ANON_PDF_AUTHOR", f"Anonymous PDF Author metadata is non-empty: {pdf_info['Author']}"))
            raw_bytes = pdf_path.read_bytes()
            if b"/PTEX.FileName" in raw_bytes:
                issues.append(Issue("ERROR", "K_ANON_PTEX_FILENAME", f"{pdf_path.name} leaks embedded /PTEX.FileName graphic paths."))
            if shutil.which("pdftotext"):
                res = subprocess.run(["pdftotext", str(pdf_path), "-"], capture_output=True, text=True, check=False)
                full_low = res.stdout.lower()
                leak_tokens = extract_private_leak_tokens(tex_path) + ["see the latest version at", "mailto:"]
                for tok in leak_tokens:
                    if tok and tok in full_low:
                        issues.append(Issue("ERROR", "K_ANON_TEXT_LEAK", f"{pdf_path.name} leaks identifying token `{tok}` in anonymous mode."))

    passed = not any(i.severity == "ERROR" for i in issues)
    return CheckReport(
        target=str(tex_path),
        mode=mode,
        anonymous_mode=anonymous,
        style_resolved=style_resolved,
        files_scanned=len(files),
        tables_checked=tables_checked,
        figures_checked=figures_checked,
        has_online_appendix=has_oa,
        passed=passed,
        issues=issues,
        pdf_info=pdf_info,
    )


# ---------------------------------------------------------------------------
# Packing: Overleaf and Lattice zips, arXiv tar.gz, public export
# ---------------------------------------------------------------------------

def bundle_overleaf(src_dir: Path, out_zip: Path, verify: bool = False) -> Path:
    with tempfile.TemporaryDirectory(prefix="ecofin_overleaf_") as tmp:
        stage = Path(tmp) / "overleaf"
        copy_project_closure(src_dir, stage, exclude_private=False, include_fonts=True)
        if (stage / "master.tex").is_file() and not (stage / "main.tex").exists():
            shutil.move(str(stage / "master.tex"), str(stage / "main.tex"))

        rel_names = [
            p.relative_to(stage).as_posix()
            for p in sorted(stage.rglob("*"))
            if p.is_file() and not p.name.startswith(".")
        ]

        out_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for arcname, fpath in checked_members(stage, rel_names):
                zi = zipfile.ZipInfo(arcname, date_time=(2026, 11, 12, 0, 0, 0))
                zi.external_attr = 0o644 << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(zi, fpath.read_bytes())

    if verify:
        with tempfile.TemporaryDirectory(prefix="ecofin_verify_ol_") as vtmp:
            vdir = Path(vtmp)
            with zipfile.ZipFile(out_zip, "r") as zf:
                zf.extractall(vdir)
            eng = "lualatex" if shutil.which("lualatex") else "pdflatex"
            compile_tex_in_stage(vdir, "main.tex", engine=eng, run_bibtex=True)
            rep = check_manuscript(vdir / "main.tex", require_built=True)
            if not rep.passed:
                raise RuntimeError(f"Overleaf bundle verification failed: {rep.issues}")
    return out_zip


def bundle_lattice(src_dir: Path, out_zip: Path, verify: bool = False) -> Path:
    """Pack a starter folder that carries a Lattice project file (.research/project.json)."""
    with tempfile.TemporaryDirectory(prefix="ecofin_lattice_") as tmp:
        stage = Path(tmp) / "ecofin-paper"
        copy_project_closure(src_dir, stage, exclude_private=False, include_fonts=True)
        res_dir = stage / ".research"
        res_dir.mkdir(parents=True, exist_ok=True)
        (res_dir / ".gitignore").write_text("cache/\nsessions/\nhistory/\n", encoding="utf-8")
        main_doc = "master.tex" if (stage / "master.tex").is_file() else "main.tex"
        proj_cfg = {
            "schemaVersion": 2,
            "projectId": "ecofin-sty-starter",
            "name": "Working paper (ecofin.sty)",
            "rootDocuments": [
                {
                    "path": main_doc,
                    "name": "Working Paper (master)",
                    "isDefault": True,
                    "chosen": True,
                }
            ],
            "primaryBibliography": "references.bib",
            "trusted": False,
            "engine": "xelatex",
            "venue": "blank",
            "wordBudget": None,
            "pageBudget": None,
            "spellingWords": [],
        }
        (res_dir / "project.json").write_text(json.dumps(proj_cfg, indent=2) + "\n", encoding="utf-8")

        rel_names = [
            p.relative_to(stage).as_posix()
            for p in sorted(stage.rglob("*"))
            if p.is_file() and p.name != ".DS_Store"
        ]

        out_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for arcname, fpath in checked_members(stage, rel_names):
                zi = zipfile.ZipInfo(arcname, date_time=(2026, 11, 12, 0, 0, 0))
                zi.external_attr = 0o644 << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(zi, fpath.read_bytes())

    if verify:
        with tempfile.TemporaryDirectory(prefix="ecofin_verify_lat_") as vtmp:
            vdir = Path(vtmp)
            with zipfile.ZipFile(out_zip, "r") as zf:
                zf.extractall(vdir)
            if not (vdir / ".research" / "project.json").is_file():
                raise RuntimeError("Lattice bundle missing .research/project.json")
            eng = "xelatex" if shutil.which("xelatex") else "pdflatex"
            compile_tex_in_stage(vdir, "master.tex", engine=eng, run_bibtex=True)
            rep = check_manuscript(vdir / "master.tex", require_built=True)
            if not rep.passed:
                raise RuntimeError(f"Lattice bundle verification failed: {rep.issues}")
    return out_zip


def bundle_arxiv(src_dir: Path, out_tar_gz: Path, verify: bool = False) -> Path:
    with tempfile.TemporaryDirectory(prefix="ecofin_arxiv_") as tmp:
        build_stage = Path(tmp) / "compile"
        copy_project_closure(src_dir, build_stage, exclude_private=False, include_fonts=False)
        if (build_stage / "master.tex").is_file():
            shutil.move(str(build_stage / "master.tex"), str(build_stage / "main.tex"))
        compile_tex_in_stage(build_stage, "main.tex", engine="pdflatex", run_bibtex=True)

        pkg_stage = Path(tmp) / "arxiv_pkg"
        pkg_stage.mkdir(parents=True, exist_ok=True)
        keep_exts = {".tex", ".sty", ".bbl", ".pdf", ".png", ".jpg", ".eps"}
        generated_pdfs = {"main.pdf", "1_title_page.pdf", "2_manuscript_anonymous.pdf", "3_online_appendix.pdf"}

        for p in sorted(build_stage.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(build_stage)
            if rel.parts[0] in {"xref", "fonts"} or p.name in generated_pdfs:
                continue
            if rel.name in {"1_title_page.tex", "2_manuscript_anonymous.tex", "3_online_appendix.tex"}:
                continue
            if p.suffix.lower() in keep_exts:
                dst = pkg_stage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)

        arxiv_readme = {
            "sources": [{"filename": "main.tex", "usage": "toplevel"}],
            "process": {"compiler": "pdflatex"},
            "spec_version": 1,
        }
        (pkg_stage / "00README.json").write_text(json.dumps(arxiv_readme, indent=2) + "\n", encoding="utf-8")

        rel_names = [p.relative_to(pkg_stage).as_posix() for p in sorted(pkg_stage.rglob("*")) if p.is_file()]
        out_tar_gz.parent.mkdir(parents=True, exist_ok=True)
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
            for arcname, fpath in checked_members(pkg_stage, rel_names):
                data = fpath.read_bytes()
                ti = tarfile.TarInfo(name=arcname)
                ti.size = len(data)
                ti.mtime = FIXED_MTIME
                ti.mode = 0o644
                ti.uid = 0
                ti.gid = 0
                ti.uname = ""
                ti.gname = ""
                tf.addfile(ti, io.BytesIO(data))
        with gzip.GzipFile(filename="", mode="wb", fileobj=out_tar_gz.open("wb"), mtime=FIXED_MTIME) as gz:
            gz.write(tar_buf.getvalue())

    if verify:
        with tempfile.TemporaryDirectory(prefix="ecofin_verify_ax_") as vtmp:
            vdir = Path(vtmp)
            with tarfile.open(out_tar_gz, "r:gz") as tf:
                if sys.version_info >= (3, 12):
                    tf.extractall(vdir, filter="data")
                else:
                    tf.extractall(vdir)
            compile_tex_in_stage(vdir, "main.tex", engine="pdflatex", run_bibtex=False)
            rep = check_manuscript(vdir / "main.tex", require_built=True)
            if not rep.passed:
                raise RuntimeError(f"arXiv bundle verification failed: {rep.issues}")
    return out_tar_gz


def export_public_repo(dest_dir: Path) -> Path:
    dest_dir = dest_dir.resolve()
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    members = list(PUBLIC_ALLOWLIST)

    for arcname, fpath in checked_members(SUITE_DIR, members):
        raw = fpath.read_bytes()
        if fpath.suffix.lower() not in {".ttf", ".otf", ".woff", ".woff2", ".pdf", ".png", ".jpg", ".webp", ".zip", ".gz"}:
            txt = raw.decode("utf-8")
            for pat, label in FORBIDDEN_PUBLIC_PATTERNS:
                if re.search(pat, txt):
                    raise RuntimeError(f"Forbidden {label} (`{pat}`) found in `{arcname}`!")
        out_p = dest_dir / arcname
        out_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fpath, out_p)

    return dest_dir


# ---------------------------------------------------------------------------
# doctor, init and the tests
# ---------------------------------------------------------------------------

def cmd_doctor() -> int:
    print("ecofin.sty: checking TeX programs and fonts")
    ok = True
    for tool in (*ENGINES, "bibtex", "latexmk", *POPPLER_TOOLS):
        loc = shutil.which(tool)
        status = f"OK ({loc})" if loc else "MISSING"
        print(f"  {tool:12s}: {status}")
        if tool in ("pdflatex", "bibtex", *POPPLER_TOOLS) and not loc:
            ok = False
    for frel in ("fonts/GeistSerif-Regular.ttf", "fonts/GeistSerif-Variable.ttf", "fonts/geist-OFL.txt"):
        p = SUITE_DIR / frel
        exists = p.is_file() and p.stat().st_size > 0
        print(f"  {frel:30s}: {'OK' if exists else 'MISSING'}")
        if not exists:
            ok = False
    return 0 if ok else 1


def init_project(target_dir: Path) -> Path:
    copy_project_closure(SAMPLE_DIR, target_dir, exclude_private=False, include_fonts=True)
    return target_dir


def run_test_all() -> int:
    with tempfile.TemporaryDirectory(prefix="ecofin_matrix_") as tmp:
        root = Path(tmp)
        proj = init_project(root / "demo_paper")

        # 1. Working paper with each engine on this computer
        for eng in ENGINES:
            if not shutil.which(eng):
                continue
            wp_out = root / f"wp_{eng}"
            build_wp(proj, wp_out, engine=eng, include_fonts=True)
            rep = check_manuscript(wp_out / "master.tex", require_built=True)
            if not rep.passed:
                print(f"[FAIL] Working Paper ({eng}): {rep.issues}")
                return 1
            print(f"[PASS] working paper ({eng})")

        # 2. Four authors, switches, all text in Geist Serif
        if shutil.which("lualatex"):
            a4_src = init_project(root / "authors4_src")
            m_txt = (a4_src / "master.tex").read_text(encoding="utf-8")
            m_txt = m_txt.replace(r"\usepackage{ecofin}", r"\usepackage[geistserif]{ecofin}")
            for off, on in (
                ("% \\author[3]{Third Author}", "\\author[3]{Third Author}"),
                ("% \\author[4]{Fourth Author}", "\\author[4]{Fourth Author}"),
                ("% \\affil[3]{Example Institute}", "\\affil[3]{Example Institute}"),
                ("% \\affil[4]{Example College}", "\\affil[4]{Example College}"),
            ):
                if off not in m_txt:
                    print(f"[FAIL] Sample master.tex no longer contains `{off}`")
                    return 1
                m_txt = m_txt.replace(off, on)
            m_txt = m_txt.replace("showoa         = true", "showoa         = false")
            m_txt = m_txt.replace(r"\ecofintoggle{extra_notes}{false}", r"\ecofintoggle{extra_notes}{true}")
            (a4_src / "master.tex").write_text(m_txt, encoding="utf-8")
            build_wp(a4_src, root / "wp_authors4", engine="lualatex", include_fonts=True)
            rep_a4 = check_manuscript(root / "wp_authors4" / "master.tex", require_built=True)
            if not rep_a4.passed:
                print(f"[FAIL] 4-Author Grid + Show/Hide Toggles: {rep_a4.issues}")
                return 1
            a4_text = subprocess.run(
                ["pdftotext", str(root / "wp_authors4" / "master.pdf"), "-"], capture_output=True, text=True
            ).stdout.lower()
            if "third author" not in a4_text or "fourth author" not in a4_text:
                print("[FAIL] 4-Author Grid: third and fourth author are not on the title page")
                return 1
            print("[PASS] four authors, a section switched on, appendix hidden, Geist Serif text (lualatex)")

            # The anonymous copy from the same master.tex
            build_wp(proj, root / "wp_blind", engine="lualatex", include_fonts=True, mode="blind")
            rep_bl = check_manuscript(root / "wp_blind" / "master.tex", anonymous=True, require_built=True)
            if not rep_bl.passed:
                print(f"[FAIL] Single-file mode=blind: {rep_bl.issues}")
                return 1
            print("[PASS] anonymous copy: no names in the text or the PDF details")

        # 2b. Every mode from the same master.tex (standard LaTeX front matter)
        if shutil.which("lualatex"):
            for md in MODES:
                out_md = root / f"mode_{md}"
                build_wp(proj, out_md, engine="lualatex", include_fonts=True, mode=md)
                anon = md.startswith("blind")
                rep_md = check_manuscript(out_md / "master.tex", anonymous=anon, require_built=True)
                if not rep_md.passed:
                    print(f"[FAIL] mode={md}: {rep_md.issues}")
                    return 1
                has_names = bool(rep_md.pdf_info.get("Author", "").strip())
                if has_names == anon:
                    print(f"[FAIL] mode={md}: PDF author field is `{rep_md.pdf_info.get('Author', '')}`")
                    return 1
            print(f"[PASS] all {len(MODES)} modes from one master.tex (lualatex)")

            # 2c. Front-matter variants, left equation numbers, and co-author notes shown / hidden
            variants = {
                "ecofin_commands": (
                    "",
                    "\\ecofintitle{A Title}\n\\ecofinauthor[corresponding]{Ada Example}{Test University}{ada@example.org}\n"
                    "\\ecofindate{November 2026}\n\\ecofinabstract{An abstract.}\n",
                    "\\ecofinmaketitle\n",
                ),
                "plain_latex_authors": (
                    "",
                    "\\title{A Title}\n\\author{Ada Example\\thanks{Test University.} \\and Ben Sample}\n\\date{November 2026}\n",
                    "\\maketitle\n\\begin{abstract}An abstract.\\end{abstract}\n",
                ),
                "eqnum_left": (
                    "[eqnum=left]",
                    "\\title{A Title}\n\\author[1]{Ada Example}\n\\affil[1]{Test University}\n",
                    "\\maketitle\n\\begin{abstract}An abstract.\\end{abstract}\n",
                ),
            }
            note_body = (
                "A first sentence sets the scene for the reader.\\mnote[FK]{Check the clustering level.} "
                "A second sentence \\replace[BS]{goes on}{continues} for long enough to fill two or three lines of the "
                "page at the normal measure, so that a moved line break would be seen. \\discuss[R2]{Add the first stage?} "
                "A third sentence closes the paragraph.\n\\begin{equation}y=\\beta x\\end{equation}\n"
            )
            for vname, (pkgopt, front, title_cmds) in variants.items():
                for notes in ("show", "hide"):
                    vdir = root / f"variant_{vname}_{notes}"
                    vdir.mkdir(parents=True)
                    (vdir / "master.tex").write_text(
                        "\\documentclass[letterpaper,11pt]{article}\n"
                        f"\\usepackage{pkgopt}{{ecofin}}\n"
                        f"\\ecofinsetup{{notes={notes},dualbib=false}}\n"
                        + front
                        + "\\begin{document}\n"
                        + title_cmds
                        + "\\section{Text}\n"
                        + note_body
                        + "\\end{document}\n",
                        encoding="utf-8",
                    )
                    build_wp(vdir, root / f"out_{vname}_{notes}", engine="lualatex", include_fonts=True)
                    rep_v = check_manuscript(root / f"out_{vname}_{notes}" / "master.tex", require_built=True)
                    if not rep_v.passed:
                        print(f"[FAIL] {vname} (notes={notes}): {rep_v.issues}")
                        return 1
                texts = {}
                for notes in ("show", "hide"):
                    texts[notes] = subprocess.run(
                        ["pdftotext", "-bbox", str(root / f"out_{vname}_{notes}" / "master.pdf"), "-"],
                        capture_output=True,
                        text=True,
                    ).stdout
                if "[FK]" not in texts["show"] or "[FK]" in texts["hide"]:
                    print(f"[FAIL] {vname}: co-author notes do not follow notes=show|hide")
                    return 1

                def _body_words(xml: str) -> list[tuple[str, int]]:
                    words = re.findall(r'<word xMin="([\d.]+)" yMin="[\d.]+" xMax="[\d.]+" yMax="([\d.]+)">([^<]*)</word>', xml)
                    keep = []
                    for x, y, w in words:
                        w = re.sub("[\u2022\u25b6\u25ba\u25b8]", "", w)
                        if w and 70 < float(x) < 541:
                            keep.append((w, round(float(x))))
                    return keep

                if _body_words(texts["show"]) != _body_words(texts["hide"]):
                    print(f"[FAIL] {vname}: a word moved when the co-author notes were switched")
                    return 1
                if vname == "eqnum_left":
                    m_tag = re.search(r'<word xMin="([\d.]+)"[^>]*>\(1\)</word>', texts["hide"])
                    if not m_tag or float(m_tag.group(1)) > 150:
                        print("[FAIL] eqnum=left: the equation number is not at the left edge")
                        return 1
            print("[PASS] \\ecofin... commands, plain LaTeX authors, eqnum=left; notes shown and hidden, no word moved")

            # 2d. Contents printed once with \maketitle and the abstract environment
            toc_dir = root / "variant_toc"
            toc_dir.mkdir(parents=True)
            (toc_dir / "master.tex").write_text(
                "\\documentclass[11pt]{article}\n\\usepackage{ecofin}\n\\ecofinsetup{showtoc=true,dualbib=false}\n"
                "\\title{A Title}\n\\author[1]{Ada Example}\n\\affil[1]{Test University}\n\\begin{document}\n\\maketitle\n"
                "\\begin{abstract}An abstract.\\end{abstract}\n\\section{One}Text.\n\\section{Two}Text.\n\\end{document}\n",
                encoding="utf-8",
            )
            build_wp(toc_dir, root / "out_toc", engine="lualatex", include_fonts=True)
            toc_text = subprocess.run(["pdftotext", str(root / "out_toc" / "master.pdf"), "-"], capture_output=True, text=True).stdout
            if toc_text.count("Contents") != 1:
                print(f"[FAIL] contents printed {toc_text.count('Contents')} times with showtoc = true")
                return 1
            print("[PASS] contents printed once with \\maketitle and the abstract environment")

        # 3. Three-file set; appendix equations continue the paper's numbering
        split_out = root / "split_pdflatex"
        build_split(proj, split_out, engine_anon="pdflatex")
        for stem, is_anon in (
            ("1_title_page", False),
            ("2_manuscript_anonymous", True),
            ("3_online_appendix", True),
        ):
            rep_s = check_manuscript(split_out / "master.tex", anonymous=is_anon, require_built=True, jobname=stem)
            if not rep_s.passed:
                print(f"[FAIL] Split {stem}: {rep_s.issues}")
                return 1
        oa_aux = (split_out / "3_online_appendix.aux").read_text(encoding="utf-8")
        ms_aux = (split_out / "2_manuscript_anonymous.aux").read_text(encoding="utf-8")
        main_eqs = [int(n) for lab, n in re.findall(r"\\newlabel\{(eq:[^}]+)\}\{\{(\d+)\}", ms_aux) if not lab.startswith("eq:oa_")]
        expected = max(main_eqs, default=0) + 1
        if rf"\newlabel{{eq:oa_decomp}}{{{{{expected}}}" not in oa_aux:
            print(f"[FAIL] Split Online Appendix equation counter did not continue at ({expected})!")
            return 1
        print("[PASS] three-file set: title page, anonymous manuscript, online appendix")

        # 4. Starter and upload files, each built again from a fresh folder
        bundle_overleaf(proj, root / "overleaf.zip", verify=True)
        print("[PASS] Overleaf starter builds from a fresh folder")
        bundle_lattice(proj, root / "lattice.zip", verify=True)
        print("[PASS] Lattice starter builds from a fresh folder")
        bundle_arxiv(proj, root / "arxiv.tar.gz", verify=True)
        print("[PASS] arXiv upload file builds without BibTeX")

        # 5. Public export
        export_public_repo(root / "public_repo_export")
        print(f"[PASS] public export: {len(PUBLIC_ALLOWLIST)} files")

    print("\nAll tests passed.")
    return 0


def print_report(rep: CheckReport) -> None:
    verdict = "PASS" if rep.passed else "FAIL"
    errs = [i for i in rep.issues if i.severity == "ERROR"]
    warns = [i for i in rep.issues if i.severity == "WARN"]
    print(f"=== ECOFIN LATEX CHECK ({rep.mode}): {verdict} ===")
    print(f"Target:          {rep.target}")
    print(f"Style Resolved:  {rep.style_resolved or 'NOT FOUND'}")
    print(f"Anonymous Mode:  {rep.anonymous_mode} | Files in Graph: {rep.files_scanned}")
    print(f"Exhibits:        {rep.tables_checked} table(s), {rep.figures_checked} figure(s) | Online Appendix: {rep.has_online_appendix}")
    print(f"Diagnostics:     {len(errs)} error(s), {len(warns)} warning(s)")
    for i in rep.issues:
        print(f"  [{i.severity}] {i.code}: {i.message}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build, check and pack a paper written with ecofin.sty")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="Check TeX engines, Poppler utilities, and bundled OFL fonts")

    p_init = sub.add_parser("init", help="Start a new paper from sample/")
    p_init.add_argument("target_dir")

    p_build = sub.add_parser("build", help="Compile Working Paper (`wp`) or 3-PDF Double-Blind (`split`)")
    p_build.add_argument("target", choices=("wp", "split"))
    p_build.add_argument("--source", default=str(SAMPLE_DIR))
    p_build.add_argument("--out", default=None)
    p_build.add_argument("--engine", default=None, choices=ENGINES)
    p_build.add_argument("--mode", default=None, choices=MODES, help="Optional single-file mode override for `wp` target")

    p_chk = sub.add_parser("check", help="Run structure, log, font, and anonymity gate")
    p_chk.add_argument("tex_path")
    p_chk.add_argument("--anonymous", action="store_true")
    p_chk.add_argument("--require-built", action="store_true")
    p_chk.add_argument("--build-dir", default=None)
    p_chk.add_argument("--jobname", default=None)
    p_chk.add_argument("--json-out", default=None)

    p_bun = sub.add_parser("bundle", help="Create deterministic Overleaf (.zip), Lattice (.zip), or arXiv (.tar.gz) bundle")
    p_bun.add_argument("kind", choices=("overleaf", "lattice", "arxiv"))
    p_bun.add_argument("--source", default=str(SAMPLE_DIR))
    p_bun.add_argument("--out", default=None)
    p_bun.add_argument("--verify", action="store_true")

    p_exp = sub.add_parser("export-public-repo", help="Export clean allowlisted standalone GitHub repository")
    p_exp.add_argument("dest_dir")

    sub.add_parser("test-all", help="Run full multi-engine, split-build, Overleaf, arXiv, and public-export matrix")

    args = ap.parse_args()

    if args.cmd == "doctor":
        return cmd_doctor()
    if args.cmd == "init":
        out = init_project(Path(args.target_dir))
        print(f"Initialized EcoFin paper directory at {out}")
        return 0
    if args.cmd == "build":
        src = Path(args.source)
        if args.target == "wp":
            eng = args.engine or ("lualatex" if shutil.which("lualatex") else "pdflatex")
            out = Path(args.out) if args.out else SUITE_DIR / "dist" / "wp"
            pdf = build_wp(src, out, engine=eng, mode=args.mode)
            print(f"Built Manuscript ({eng}, mode={args.mode or 'default'}): {pdf}")
            return 0
        eng = args.engine or "pdflatex"
        out = Path(args.out) if args.out else SUITE_DIR / "dist" / "split"
        pdfs = build_split(src, out, engine_anon=eng)
        for k, v in pdfs.items():
            print(f"Built {k}: {v}")
        return 0
    if args.cmd == "check":
        rep = check_manuscript(
            Path(args.tex_path),
            anonymous=args.anonymous,
            require_built=args.require_built,
            build_dir=Path(args.build_dir) if args.build_dir else None,
            jobname=args.jobname,
        )
        print_report(rep)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(asdict(rep), indent=2) + "\n", encoding="utf-8")
        return 0 if rep.passed else 1
    if args.cmd == "bundle":
        src = Path(args.source)
        if args.kind == "overleaf":
            out = Path(args.out) if args.out else SUITE_DIR / "dist" / "overleaf_bundle.zip"
            bundle_overleaf(src, out, verify=args.verify)
            print(f"Wrote Overleaf bundle: {out}")
            return 0
        if args.kind == "lattice":
            out = Path(args.out) if args.out else SUITE_DIR / "dist" / "lattice_bundle.zip"
            bundle_lattice(src, out, verify=args.verify)
            print(f"Wrote Lattice bundle: {out}")
            return 0
        out = Path(args.out) if args.out else SUITE_DIR / "dist" / "arxiv_bundle.tar.gz"
        bundle_arxiv(src, out, verify=args.verify)
        print(f"Wrote arXiv bundle: {out}")
        return 0
    if args.cmd == "export-public-repo":
        out = export_public_repo(Path(args.dest_dir))
        print(f"Exported clean public GitHub repository to {out}")
        return 0
    if args.cmd == "test-all":
        return run_test_all()

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
