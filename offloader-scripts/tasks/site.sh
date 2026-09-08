#!/usr/bin/env bash
# summary: Assemble the GitHub Pages site from the reports
set -eo pipefail
# shellcheck source=../../scripts/hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/hlsl-dev.sh"

HD_TASK_DESC="Assembles the GitHub Pages site from offloader-scripts/reports/ into _site/:
copies each report's self-contained HTML (and its JSON/CSV/MD siblings, minus
the bulky logs/), prunes reports older than 30 days, and regenerates
_site/index.html -- a landing page listing every retained report newest-first.
Offline; no token needed.

This mirrors what the hourly .github/workflows/offload-report-pages.yml runs in
CI; run offloader-monitor first to have a fresh report to fold in. Open
_site/index.html to preview."
hd_parse "$@"
hd_init_root

cd "$HD_ROOT/offloader-scripts"
python3 build_site.py --reports-dir reports --site-dir _site --max-age-days 30
