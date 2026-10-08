#!/bin/bash
# usage: snap.sh <label>  -> snapshot to cw16-formal-c18/snapshots/<label>.json, compare to C18
set -e
cd /home/dwchoo/ai-exp-assistant
R=/home/dwchoo/ai-exp-assistant/.workflow/core-workbench/runs/implement-p2.6-20260927
REQ=/tmp/wb-cw16-c18/snap-req.json
echo '{"root":"/home/dwchoo/ai-exp-assistant","watch":[],"exclude":[".workflow/core-workbench/runs/implement-p2.6-20260927/**","graphify-out/**"],"checkpoint":false}' > $REQ
OUT=$R/cw16-formal-c18/snapshots/$1.json
python3 .agents/skills/workflow-ledger/scripts/workflow_tools.py snapshot --input $REQ --output $OUT >/dev/null
python3 - "$OUT" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
c="a2e3daf23dc98d8c98c760d62e227d4425c0b6b8265b6b3d9a696bc7a573482b"
print("snapshot", d["candidate"], "stable", d["stable"], "unknowns", d["unknowns"], "EQ_C18" if d["candidate"]==c else "DIFF_C18")
PY
echo "git-status: [$(git status --short -- src omp_bridge tests docs pyproject.toml | tr '\n' ' ')]"
