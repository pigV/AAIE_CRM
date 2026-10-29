#!/bin/zsh
set -eu
cd "${0:A:h}"
"/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" -B tools/test_copy_control.py status
