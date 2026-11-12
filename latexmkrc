# latexmk settings for a paper written with ecofin.sty (also read by Overleaf).
# The second reference list needs BibTeX runs on bu1.aux, which latexmk finds by itself.

$bibtex_use = 1;
$bibtex_fudge = 1;
$max_repeat = 6;

push @generated_exts, 'bbl', 'blg', 'nav', 'snm', 'vrb', 'fls', 'fdb_latexmk', 'synctex.gz';
$clean_ext .= ' bu*.aux bu*.bbl bu*.blg xref/*.aux xref/*.tex';
