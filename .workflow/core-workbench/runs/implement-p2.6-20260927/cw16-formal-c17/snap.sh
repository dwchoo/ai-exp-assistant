#!/bin/bash
set -e
cd /home/dwchoo/ai-exp-assistant
R=/home/dwchoo/ai-exp-assistant/.workflow/core-workbench/runs/implement-p2.6-20260927
REQ=/tmp/wb-cw16-c17/snap-req.json
echo '{"root":"/home/dwchoo/ai-exp-assistant","watch":[],"exclude":[".workflow/core-workbench/runs/implement-p2.6-20260927/**","graphify-out/**"],"checkpoint":false}' > $REQ
OUT=$R/cw16-formal-c17/snapshots/$1.json
python3 .agents/skills/workflow-ledger/scripts/workflow_tools.py snapshot --input $REQ --output $OUT >/dev/null
python3 - "$OUT" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
c="db39f4aabd7034dd280110ac93a6b5d0577440dd30981cd17f9883404259767d"
print("snapshot", d["candidate"], "stable", d["stable"], "unknowns", d["unknowns"], "EQ_C17" if d["candidate"]==c else "DIFF_C17")
PY
echo "git-status: [$(git status --short -- src omp_bridge tests docs pyproject.toml | tr '\n' ' ')]"
