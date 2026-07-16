#!/bin/bash
# Coverage-gap diagnostic (run ON the DGX).
#
# The NOE share is mounted twice with DIFFERENT SMB credentials:
#   /ICFO/groups/NOE  -> cred `yzamir`    (restricted; nightly scan reads THIS)
#   /mnt/noe          -> cred `sberlanga` (uid=qnoe-ai; broad access)
# yzamir can read /mnt/noe (dir_mode 0755), so the SMB cred — not the local user —
# is what differs. Folders readable via /mnt/noe but NOT via /ICFO are silently
# missing from the index. See memory/mistakes (CavityQED, 2026-07-16) + TODO.
#
# Usage: bash scripts/coverage_gap.sh [maxdepth]   (default depth 3)
#   Lines printed = folders that SHOULD be indexed but aren't (fix: scan /mnt/noe).
#   A folder denied on BOTH mounts would NOT appear here — that WOULD need an ACL grant.
set -euo pipefail
DEPTH="${1:-3}"
comm -12 \
  <(find /ICFO/groups/NOE -maxdepth "$DEPTH" -type d ! -readable 2>/dev/null | sed 's|^/ICFO/groups/NOE/||' | sort) \
  <(find /mnt/noe          -maxdepth "$DEPTH" -type d   -readable 2>/dev/null | sed 's|^/mnt/noe/||'          | sort)
