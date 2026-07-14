#!/bin/bash
# ExecStopPost for qnoe-hermes-sandbox.service — make sure a unit stop never
# orphans a Teams-polling container.
source /home/qnoe-ai/.profile
export OPENSHELL_LOCAL_TLS_DIR=/home/qnoe-ai/.local/state/openshell/tls
openshell sandbox delete qnoe-hermes 2>/dev/null || true
exit 0
