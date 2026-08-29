#!/usr/bin/env bash
# Launch PiStorm Imager straight from a checkout, without installing anything.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec python3 -m pistorm_imager "$@"
