#!/bin/sh
# The whole suite, plus the mutation run that says whether the suite is worth
# anything. A test nobody has watched fail is not a test.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
export PYTHONPATH="$HERE:$HERE/tests"

echo "== unit tests =="
python3 -m unittest discover -s "$HERE/tests" -p 'test_*.py' "$@"

echo
echo "== mutation run =="
python3 "$HERE/tools/mutate.py"
