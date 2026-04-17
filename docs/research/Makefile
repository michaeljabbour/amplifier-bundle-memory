FIGS_DOT := $(wildcard figures/*.dot)
FIGS_PDF := $(FIGS_DOT:.dot=.pdf)
FIGS_PNG := $(FIGS_DOT:.dot=.png)

all: gene-transfer-v1.2.0.pdf

figures/%.pdf: figures/%.dot
	dot -Tpdf -o $@ $<

figures/%.png: figures/%.dot
	dot -Tpng -o $@ $<

gene-transfer-v1.2.0.pdf: gene-transfer-v1.2.0.tex $(FIGS_PDF)
	latexmk -pdf -interaction=nonstopmode gene-transfer-v1.2.0.tex

clean:
	latexmk -C 2>/dev/null || true
	rm -f figures/*.pdf figures/*.png
	rm -f gene-transfer-v1.2.0.pdf

.PHONY: all clean
