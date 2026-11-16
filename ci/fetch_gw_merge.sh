#!/usr/bin/env bash
# fetch_gw_merge.sh <url> <sha256> <dir>: gw_merge for Linux (xray-16/src/utils/gw_merge, job gw_merge of the CI of the
# engine) into <dir>, a binary or a tar.gz with gw_merge at its root. Prints the path of gw_merge.
#
# <url> (vars.GW_MERGE_URL, optional): empty - DEFAULT_URL, the asset of the latest release of the engine. A pinned tag
#   (.../releases/download/<tag>/gw_merge-linux-x86_64.tar.gz) holds the catalog on one gw_merge.
# <sha256> (vars.GW_MERGE_SHA256, optional): empty - the hash comes from the same release, the file <url>.sha256 that
#   the CI of the engine publishes beside the archive (sha256sum format: "<64 hex>  <name>"; the first 64 hex digits
#   are taken, any case). Decision of the developer 2026-11-14: a hash set by hand (review F4) is not required; the
#   check stays against a broken or partial download.
# A .../releases/latest/download/<name> address is resolved to the tag it points at first, so the archive and its
# .sha256 come from one release even when a new release appears between the two downloads.
# Nothing is unpacked or made executable before the download has the sha256. Any failure - exit 1 with an ::error::
# (the caller decides: build-index goes on without gw_merge, recompute-deltas fails). <dir>/gw_merge.url - the address
# used (with the resolved tag), for ci/gw_merge_info.py.
set -euo pipefail

DEFAULT_URL="https://github.com/ChugunovRoman/xray-16/releases/latest/download/gw_merge-linux-x86_64.tar.gz"
MAX_FILE=268435456  # 256 MiB
MAX_SHA_FILE=4096

# parse_sha256 <file>: the first 64 hex digits at the start of the first line, lower case; nothing (exit 1) - not a
# sha256 (fewer or more than 64 hex digits, another text)
parse_sha256() {
  local line=""
  IFS= read -r line < "$1" || [ -n "$line" ] || return 1
  line="${line%$'\r'}"
  if ! printf '%s\n' "$line" | grep -Eq '^[0-9A-Fa-f]{64}([^0-9A-Fa-f]|$)'; then
    return 1
  fi
  printf '%s\n' "${line:0:64}" | tr 'A-F' 'a-f'
}

# resolve_latest <url>: .../releases/latest/download/<name> -> .../releases/download/<tag>/<name> (the redirect of
# GitHub); any other url or an answer without such a redirect - the url as it is
resolve_latest() {
  local url="$1" loc
  case "$url" in
    https://github.com/*/releases/latest/download/*) ;;
    *) printf '%s\n' "$url"; return 0 ;;
  esac
  loc="$(curl -fsSI --proto '=https' --max-time 60 -o /dev/null -w '%{redirect_url}' "$url" 2> /dev/null)" || loc=""
  case "$loc" in
    https://github.com/*/releases/download/*/"${url##*/}") printf '%s\n' "$loc" ;;
    *) printf '%s\n' "$url" ;;
  esac
}

main() {
  local url="${1:-}" sha="${2:-}" dir="${3:?usage: fetch_gw_merge.sh <url> <sha256> <dir>}"
  if [ -z "$url" ]; then
    url="$DEFAULT_URL"
    if [ -n "$sha" ]; then
      echo "::warning::vars.GW_MERGE_SHA256 is set without vars.GW_MERGE_URL: the hash of one file is checked" \
           "against the latest release of the engine, the next release will not match it" >&2
    fi
  fi
  case "$url" in
    https://*) ;;
    *) echo "::error::vars.GW_MERGE_URL must be an https:// address" >&2; return 1 ;;
  esac
  url="$(resolve_latest "$url")"
  echo "gw_merge: $url" >&2
  mkdir -p "$dir"
  if [ -n "$sha" ]; then
    sha="$(printf '%s' "$sha" | tr 'A-F' 'a-f')"
    if ! printf '%s' "$sha" | grep -Eq '^[0-9a-f]{64}$'; then
      echo "::error::vars.GW_MERGE_SHA256 must be the sha256 (64 hex digits) of the file at the gw_merge URL" >&2
      return 1
    fi
    echo "gw_merge: sha256 of vars.GW_MERGE_SHA256" >&2
  else
    if ! curl -fsSL --proto '=https' --max-filesize "$MAX_SHA_FILE" "$url.sha256" -o "$dir/dl.sha256"; then
      rm -f "$dir/dl.sha256"
      echo "::error::no $url.sha256 (the release of the engine has no hash of gw_merge) and" \
           "vars.GW_MERGE_SHA256 is not set" >&2
      return 1
    fi
    if ! sha="$(parse_sha256 "$dir/dl.sha256")"; then
      rm -f "$dir/dl.sha256"
      echo "::error::$url.sha256 does not start with a sha256 (64 hex digits)" >&2
      return 1
    fi
    rm -f "$dir/dl.sha256"
    echo "gw_merge: sha256 of $url.sha256" >&2
  fi
  if ! curl -fsSL --proto '=https' --max-filesize "$MAX_FILE" "$url" -o "$dir/dl"; then
    rm -f "$dir/dl"
    echo "::error::gw_merge is not downloaded from $url" >&2
    return 1
  fi
  if ! printf '%s  %s\n' "$sha" "$dir/dl" | sha256sum -c --quiet - >&2; then
    echo "::error::the file at $url does not have the expected sha256 (a broken or partial download, or the asset" \
         "was uploaded again without its hash): not used" >&2
    rm -f "$dir/dl"
    return 1
  fi
  if gzip -t "$dir/dl" 2> /dev/null; then
    tar -xzf "$dir/dl" -C "$dir" --no-same-owner
    rm -f "$dir/dl"
  else
    mv "$dir/dl" "$dir/gw_merge"
  fi
  if [ ! -f "$dir/gw_merge" ]; then
    echo "::error::the archive at $url has no gw_merge at its root" >&2
    return 1
  fi
  chmod +x "$dir/gw_merge"
  # the address with the tag it resolved to: ci/gw_merge_info.py shows it in the Summary
  printf '%s\n' "$url" > "$dir/gw_merge.url"
  printf '%s\n' "$dir/gw_merge"
}

# sourced (the tests call parse_sha256 / resolve_latest): only the functions
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
