#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$repo_root"

PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE
PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

python -m unittest discover -s tests/contracts -v
node --experimental-strip-types --test tests/gates/harness/contracts.test.ts tests/gates/harness/ports-v2.test.ts
