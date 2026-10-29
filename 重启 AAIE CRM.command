#!/bin/zsh
set -eu
cd "${0:A:h}"
"/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" -B tools/test_copy_control.py restart
/usr/bin/open "http://127.0.0.1:8780/login"
