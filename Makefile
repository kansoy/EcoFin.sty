PYTHON ?= python3
ENGINE_WP ?= lualatex
ENGINE_SPLIT ?= pdflatex

.PHONY: help doctor wp split check overleaf arxiv figure-demo test clean

help:
	@echo "ecofin.sty"
	@echo "  make doctor       Check TeX programs, BibTeX, Poppler tools and fonts"
	@echo "  make wp           Build the working paper (ENGINE_WP=lualatex)"
	@echo "  make split        Build title page, anonymous manuscript and online appendix"
	@echo "  make check        Check structure, logs, fonts and that the anonymous files carry no names"
	@echo "  make overleaf     Pack and test the Overleaf starter (dist/overleaf_bundle.zip)"
	@echo "  make arxiv        Pack and test the arXiv upload file (dist/arxiv_bundle.tar.gz)"
	@echo "  make figure-demo  Draw the example figure with figstyle_ecofin.py"
	@echo "  make test         Run every test (LuaLaTeX, XeLaTeX, pdfLaTeX)"
	@echo "  make clean        Remove build/ and dist/"

doctor:
	$(PYTHON) build.py doctor

wp:
	$(PYTHON) build.py build wp --engine $(ENGINE_WP)

split:
	$(PYTHON) build.py build split --engine $(ENGINE_SPLIT)

check: wp split
	$(PYTHON) build.py check dist/wp/master.tex --require-built
	$(PYTHON) build.py check dist/split/master.tex --anonymous --require-built --jobname 2_manuscript_anonymous
	$(PYTHON) build.py check dist/split/master.tex --anonymous --require-built --jobname 3_online_appendix

overleaf:
	$(PYTHON) build.py bundle overleaf --verify

arxiv:
	$(PYTHON) build.py bundle arxiv --verify

figure-demo:
	$(PYTHON) figstyle_ecofin.py --demo dist/figures

test:
	$(PYTHON) build.py test-all

clean:
	rm -rf build dist
