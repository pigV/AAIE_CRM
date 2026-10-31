#!/bin/zsh
set -eu
cd "${0:A:h}"
"/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" -B tools/runtime_stack.py start --allow-production-control
/usr/bin/open "http://127.0.0.1:8780/login"
