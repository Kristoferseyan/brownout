#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
: "${DEPLOY_HOST:?set DEPLOY_HOST to an ssh host}"
git diff --quiet HEAD || { echo "commit first"; exit 1; }
git archive HEAD | ssh "$DEPLOY_HOST" 'mkdir -p ~/brownout && tar -x -C ~/brownout && cp ~/brownout/brownout.service ~/brownout/brownout.timer ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now brownout.timer && systemctl --user list-timers brownout.timer --no-pager | head -3'
