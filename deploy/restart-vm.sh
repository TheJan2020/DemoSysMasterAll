#!/usr/bin/env bash
# Bounce the FastAPI service on the VM after backend .py changes have
# synced through Mutagen. Frontend / static-asset changes are picked up
# immediately and don't need this.
#
# Usage from the project root:
#     deploy/restart-vm.sh
#
# Tail logs:
#     ssh demosys 'sudo journalctl -u pwdemo -f'

set -e
ssh demosys 'sudo systemctl restart pwdemo && sleep 1 && sudo systemctl is-active pwdemo'
