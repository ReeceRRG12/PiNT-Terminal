#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print 'Create the virtual environment first using the README instructions.'
  read '?Press Enter to close.'
  exit 1
fi
exec .venv/bin/python -m pint_terminal
