#!/bin/bash
# Final pipeline: analyze → figures → fill paper → compile PDF.
# Run after pilot completes.
set -e
set -o pipefail

cd "$(dirname "$0")"

LABEL="${1:-pilot}"
JSONL="trials/results_${LABEL}.jsonl"

if [[ ! -s "$JSONL" ]]; then
  echo "missing or empty: $JSONL"
  exit 1
fi

echo "=== 1. analyze ==="
python3 analysis/analyze.py "$JSONL"

echo ""
echo "=== 2. figures ==="
python3 analysis/make_figures.py "trials/results_${LABEL}"

echo ""
echo "=== 3. fill paper ==="
cd paper
python3 fill_paper.py \
  --paper paper.md \
  --analysis "../trials/results_${LABEL}.analysis.json" \
  --jsonl "../$JSONL" \
  --out paper_filled.md

echo ""
echo "=== 4. compile PDF (pandoc + xelatex) ==="
pandoc paper_filled.md \
  --pdf-engine=xelatex \
  --metadata=keywords:"" \
  --variable=geometry:"margin=1in" \
  -V mainfont="Helvetica" \
  -V monofont="Menlo" \
  -V documentclass="article" \
  -V fontsize="11pt" \
  -V linkcolor="blue" \
  -o paper.pdf

ls -la paper.pdf

echo ""
echo "=== Final summary ==="
cat "../trials/results_${LABEL}.analysis.md"
