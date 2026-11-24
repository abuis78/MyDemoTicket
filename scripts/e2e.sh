#!/usr/bin/env bash
# Runs the end-to-end suite (tests/e2e) N times in a row, each run with fresh
# temporary data directories. Exit code != 0 as soon as one run fails.
#
#   bash scripts/e2e.sh        # 3 runs
#   bash scripts/e2e.sh 10     # 10 runs
#   PYTHON=python3.9 bash scripts/e2e.sh 1
set -u

runs="${1:-3}"
case "$runs" in
  ''|*[!0-9]*|0) echo "usage: $0 [runs>0]" >&2; exit 2 ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
e2e_dir="$repo_root/tests/e2e"

for i in $(seq 1 "$runs"); do
  data_root="$(mktemp -d "${TMPDIR:-/tmp}/mdt-e2e-run.XXXXXX")"
  echo "E2E run $i/$runs: starting (data: $data_root)"
  if MDT_E2E_DATA_ROOT="$data_root" "$python_bin" -m unittest discover -s "$e2e_dir" -t "$e2e_dir" -v; then
    rm -rf "$data_root"
    echo "E2E run $i/$runs: OK"
  else
    rm -rf "$data_root"   # holds generated credentials, never keep it around
    echo "E2E run $i/$runs: FAILED"
    exit 1
  fi
done
echo "E2E: all $runs run(s) OK"
