#!/bin/zsh
set -eu
cd "${0:A:h}"
"/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" -B tools/runtime_stack.py stop --allow-production-control
