#!/bin/bash
set -euo pipefail
root=/home/blaze/blaze/blaze_backend
install -m 644 "$root/deploy/systemd/blaze-cancellation-analysis.service" /etc/systemd/system/blaze-cancellation-analysis.service
install -m 644 "$root/deploy/systemd/blaze-cancellation-analysis.timer" /etc/systemd/system/blaze-cancellation-analysis.timer
systemctl daemon-reload
systemctl restart blaze.service
systemctl enable --now blaze-cancellation-analysis.timer
systemctl start blaze-cancellation-analysis.service
systemctl is-active blaze.service blaze-cancellation-analysis.timer
