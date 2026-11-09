#!/usr/bin/env bash
# fetch_gw_merge.sh <url> <sha256> <dir>: gw_merge for Linux (xray-16/src/utils/gw_merge, the CI of the engine) into
# <dir>, a binary or a tar.gz with gw_merge at its root. Nothing is unpacked or made executable before the download has
# the sha256 of vars.GW_MERGE_SHA256 (review F4): the asset of a release of the engine can be uploaded again by anyone
# with write access to the engine repo, and this binary runs in the CI of the index. Prints the path of gw_merge; no
# URL - prints nothing (the versions that replace texts of the base wait). A URL without a valid sha256 - an error.
set -euo pipefail
url="${1:-}"
sha="${2:-}"
dir="${3:?usage: fetch_gw_merge.sh <url> <sha256> <dir>}"
if [ -z "$url" ]; then
  echo "::notice::vars.GW_MERGE_URL is not set: no gw_merge, the versions that replace texts of the base wait" >&2
  exit 0
fi
case "$url" in
  https://*) ;;
  *) echo "::error::vars.GW_MERGE_URL must be an https:// address" >&2; exit 1 ;;
esac
sha="$(printf '%s' "$sha" | tr 'A-F' 'a-f')"
if ! printf '%s' "$sha" | grep -Eq '^[0-9a-f]{64}$'; then
  echo "::error::vars.GW_MERGE_SHA256 must be the sha256 (64 hex digits) of the file at vars.GW_MERGE_URL" >&2
  exit 1
fi
mkdir -p "$dir"
curl -fsSL --proto '=https' --max-filesize 268435456 "$url" -o "$dir/dl"
if ! printf '%s  %s\n' "$sha" "$dir/dl" | sha256sum -c --quiet - >&2; then
  echo "::error::the file at vars.GW_MERGE_URL does not have the sha256 of vars.GW_MERGE_SHA256: not used" >&2
  rm -f "$dir/dl"
  exit 1
fi
if gzip -t "$dir/dl" 2> /dev/null; then
  tar -xzf "$dir/dl" -C "$dir" --no-same-owner
  rm -f "$dir/dl"
else
  mv "$dir/dl" "$dir/gw_merge"
fi
if [ ! -f "$dir/gw_merge" ]; then
  echo "::error::the archive at vars.GW_MERGE_URL has no gw_merge at its root" >&2
  exit 1
fi
chmod +x "$dir/gw_merge"
printf '%s\n' "$dir/gw_merge"
