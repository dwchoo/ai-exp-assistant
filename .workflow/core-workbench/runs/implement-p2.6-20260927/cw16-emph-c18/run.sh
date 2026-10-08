#!/bin/bash
# usage: run.sh <label>   (repo root; env -i, no TMUX*/HERDR_*, fake HOME, proxies closed, scripted provider)
cd /home/dwchoo/ai-exp-assistant
R=/home/dwchoo/ai-exp-assistant/.workflow/core-workbench/runs/implement-p2.6-20260927/cw16-emph-c18
L=$1
FH=$(mktemp -d /tmp/wb-emph-home-XXXXXX); mkdir -p $FH/.omp/agent
/usr/bin/env -i HOME=$FH PATH=/home/dwchoo/.local/bin:/usr/bin:/bin LANG=C.UTF-8 TERM=xterm-256color PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 all_proxy=http://127.0.0.1:9 \
  NO_PROXY=127.0.0.1 no_proxy=127.0.0.1 ${WB_EMPH_TERM:+WB_EMPH_TERM=$WB_EMPH_TERM} WB_LIVE_CW16=1 WB_CW16_REPORT_DIR=$R/reports WB_CW16_RUN_ID=c18-emph-$L WB_EMPH_OUT=$R/emph-report-$L.json \
 /tmp/cw02-g1-venv/bin/python $R/probe_emph.py -v
rc=$?; rm -rf "$FH"; echo "exit=$rc"; exit $rc
