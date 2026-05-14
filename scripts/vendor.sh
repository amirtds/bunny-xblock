#!/usr/bin/env bash
# Fetches third-party JS dependencies into bunny_xblock/static/js/vendor/.
# Run after cloning the repo (or before bumping versions) so the package
# can ship to PyPI with the vendored asset embedded.

set -euo pipefail

VENDOR_DIR="$(cd "$(dirname "$0")/.." && pwd)/bunny_xblock/static/js/vendor"
TUS_VERSION="4.3.1"
TUS_URL="https://unpkg.com/tus-js-client@${TUS_VERSION}/dist/tus.min.js"

mkdir -p "${VENDOR_DIR}"
echo "[vendor] tus-js-client@${TUS_VERSION} -> ${VENDOR_DIR}/tus.min.js"
curl -fSL "${TUS_URL}" -o "${VENDOR_DIR}/tus.min.js"
echo "[vendor] done"
