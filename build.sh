#!/usr/bin/env bash
export PLAYWRIGHT_BROWSERS_PATH=0
set -o errexit
pip install -r requirements.txt
playwright install chromium
playwright install chromium-headless-shell
