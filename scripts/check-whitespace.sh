#!/usr/bin/env bash
# Fail on whitespace errors: trailing blanks, a space before a tab, or blank lines at end
# of file. Checks the committed tree as a whole, not only the current diff, so a problem
# introduced before this script existed is still caught.
set -uo pipefail

status=0
empty_tree=$(git hash-object -t tree /dev/null)

git diff --check "$empty_tree" HEAD || status=1
git diff --check || status=1
git diff --cached --check || status=1

if [ "$status" -eq 0 ]; then
  echo "whitespace clean"
fi
exit "$status"
