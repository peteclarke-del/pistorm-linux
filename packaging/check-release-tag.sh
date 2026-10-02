#!/usr/bin/env bash
#
# A release is two statements of one version: the tag, and the package's
# __version__. This refuses a tag that disagrees, before anything is built or
# published.

set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' \
    "${project_dir}/pistorm_imager/__init__.py")"
release_tag="${1:-}"

if [[ "${release_tag}" != "v${runtime_version}" ]]; then
    echo "Release tag ${release_tag:-<missing>} does not match version v${runtime_version}." >&2
    exit 1
fi

echo "Release tag ${release_tag} and the package both say ${runtime_version}."
