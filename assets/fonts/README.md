# Label font

`TeXGyreHeros-Bold.otf` is the font every label is drawn with.

It is a metric-compatible clone of Helvetica, which is what the PDF path
used and what the first approved physical labels were printed in. Keeping
the font in the repo rather than using a system font means a label renders
identically on a developer's machine, inside the container, and in a test;
a missing or substituted system font would silently change what prints.

Licence: the GUST Font License (`GUST-FONT-LICENSE.txt`), an LPPL-style
licence that permits redistribution. The font is from the TeX Gyre project
by GUST, the Polish TeX Users Group.
