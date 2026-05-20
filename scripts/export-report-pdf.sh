#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 REPORT.md [REPORT.pdf]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INPUT_MD="$(realpath "$1")"

if [[ ! -f "${INPUT_MD}" ]]; then
  echo "Markdown file not found: ${INPUT_MD}" >&2
  exit 1
fi

if [[ $# -eq 2 ]]; then
  OUTPUT_PDF="$(realpath -m "$2")"
else
  OUTPUT_PDF="${INPUT_MD%.md}.pdf"
fi

TMP_HTML="${INPUT_MD%.md}_tmp.html"
PYTHON="${PYTHON_BIN:-${PYTHON:-python}}"

"${PYTHON}" - "${REPO_ROOT}" "${INPUT_MD}" "${TMP_HTML}" <<'PY'
import pathlib
import sys

repo_root = pathlib.Path(sys.argv[1])
input_md = pathlib.Path(sys.argv[2])
tmp_html = pathlib.Path(sys.argv[3])

sys.path.insert(0, str(repo_root))

from src.formatters import markdown_to_html_document

markdown_text = input_md.read_text(encoding="utf-8")
html = markdown_to_html_document(markdown_text)
tmp_html.write_text(html, encoding="utf-8")
PY

"${SCRIPT_DIR}/chromium-markdown-pdf.sh" \
  --headless \
  --disable-gpu \
  "--print-to-pdf=${OUTPUT_PDF}" \
  "file://${TMP_HTML}"

echo "${OUTPUT_PDF}"
