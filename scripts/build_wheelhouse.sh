#!/usr/bin/env bash
# Build datatrust-mcp wheel + offline wheelhouse for DataTrust wwwroot/mcp/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/../DataTrust/DataTrustFrontEnd/DataTrustWebApp/wwwroot/mcp}"

echo "[build_wheelhouse] building datatrust-mcp wheel"
cd "$ROOT"
rm -rf dist
python3 -m build --wheel

mkdir -p "$OUT"
rm -f "$OUT"/*.whl

WHEEL=(dist/datatrust_mcp-*.whl)
if [ ! -f "${WHEEL[0]}" ]; then
  echo "ERROR: no datatrust_mcp wheel in dist/" >&2
  exit 1
fi

echo "[build_wheelhouse] downloading transitive deps for 4 platforms"
for PLAT in macosx_11_0_arm64 macosx_11_0_x86_64 manylinux2014_x86_64 win_amd64; do
  pip download \
    --dest "$OUT" \
    --python-version 310 \
    --platform "$PLAT" \
    --only-binary=:all: \
    "${WHEEL[0]}"
done

echo "[build_wheelhouse] done — $(ls -1 "$OUT"/*.whl | wc -l | tr -d ' ') wheels in $OUT"
