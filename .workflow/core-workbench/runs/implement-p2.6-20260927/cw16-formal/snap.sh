#!/bin/bash
# usage: snap.sh <label>  -> writes snapshot to run dir cw16-formal/snapshots/<label>.json and compares to C16
set -e
cd /home/dwchoo/ai-exp-assistant
R=/home/dwchoo/ai-exp-assistant/.workflow/core-workbench/runs/implement-p2.6-20260927
REQ=/tmp/wb-cw16-b4b/snap-req.json
echo '{"root":"/home/dwchoo/ai-exp-assistant","watch":[],"exclude":[".workflow/core-workbench/runs/implement-p2.6-20260927/**","graphify-out/**"],"checkpoint":false}' > $REQ
OUT=$R/cw16-formal/snapshots/$1.json
python3 .agents/skills/workflow-ledger/scripts/workflow_tools.py snapshot --input $REQ --output $OUT >/dev/null
python3 - "$OUT" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
c="f8ad6ec989ec2b9f1f72a81414bbaeeadc911997a196c0edf1eeed18442f4e3d"
print("snapshot", d["candidate"], "stable", d["stable"], "unknowns", d["unknowns"], "EQ_C16" if d["candidate"]==c else "DIFF_C16")
PY
echo "git-status: [$(git status --short -- src omp_bridge tests docs pyproject.toml | tr '\n' ' ')]"
