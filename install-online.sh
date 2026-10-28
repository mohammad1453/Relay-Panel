#!/usr/bin/env bash
# Relay online installer.
#
#   bash <(curl -Ls https://raw.githubusercontent.com/mohammad1453/Relay-Panel/main/install-online.sh)
#
# Downloads a published Relay release from GitHub, checks its SHA-256, then
# installs Relay, or updates it when Relay is already installed. Options:
#
#   --version vX.Y.Z   install that release instead of the latest one
#   --uninstall        remove Relay (data is kept unless you add --purge-data)
#   --proxy URL        download through an HTTP proxy, e.g. --proxy http://127.0.0.1:10808
#                      (for servers that cannot reach GitHub or pypi.org directly)
#   anything else      passed to install.sh, e.g. --public-http --origin ... --port 8095
#
# Releases are built by GitHub Actions from version tags; see docs/RELEASING.md.
set -euo pipefail

REPO="mohammad1453/Relay-Panel"
CODE_DIR="/opt/3xui-reseller"
VERSION=""
UNINSTALL=0
PROXY=""
PASS=()

red() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { red "Error: $*"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --version) [ $# -ge 2 ] || die "--version needs a value like v1.0.0"; VERSION="$2"; shift 2 ;;
    --version=*) VERSION="${1#*=}"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --proxy) [ $# -ge 2 ] || die "--proxy needs a value like http://127.0.0.1:10808"; PROXY="$2"; shift 2 ;;
    --proxy=*) PROXY="${1#*=}"; shift ;;
    -h|--help) sed -n '2,17p' "$0" 2>/dev/null || true; exit 0 ;;
    *) PASS+=("$1"); shift ;;
  esac
done

[ "$(uname -s)" = "Linux" ] || die "Relay installs on a Linux VPS."

if [ -n "$PROXY" ]; then
  case "$PROXY" in
    http://*|https://*) ;;
    *) die "--proxy must start with http:// (a 3X-UI 'mixed' or 'http' inbound works), e.g. http://127.0.0.1:10808" ;;
  esac
  # curl and pip download through the proxy; local checks on this server never use it.
  export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
  export no_proxy="127.0.0.1,localhost,::1" NO_PROXY="127.0.0.1,localhost,::1"
  info "Downloading through the proxy $PROXY"
fi
[ "$(id -u)" -eq 0 ] || die "Run as root, for example: sudo bash <(curl -Ls https://raw.githubusercontent.com/$REPO/main/install-online.sh)"

if [ "$UNINSTALL" -eq 1 ]; then
  [ -f "$CODE_DIR/uninstall.sh" ] || die "Relay is not installed in $CODE_DIR."
  exec bash "$CODE_DIR/uninstall.sh" "${PASS[@]+"${PASS[@]}"}"
fi

need=()
command -v curl >/dev/null || need+=(curl)
command -v tar >/dev/null || need+=(tar)
command -v python3 >/dev/null || need+=(python3)
command -v sha256sum >/dev/null || need+=(coreutils)
if [ ${#need[@]} -gt 0 ]; then
  command -v apt-get >/dev/null || die "Please install: ${need[*]}"
  info "Installing ${need[*]}"
  NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive apt-get update -qq
  NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" ca-certificates >/dev/null
fi

if [ -z "$VERSION" ]; then
  info "Looking up the latest Relay release"
  VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name",""))' 2>/dev/null || true)"
  [ -n "$VERSION" ] || die "No published release found at https://github.com/$REPO/releases"
fi
case "$VERSION" in v*) ;; *) VERSION="v$VERSION" ;; esac
printf '%s' "$VERSION" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || die "Version must look like v1.0.0, got: $VERSION"

WORK="$(mktemp -d /tmp/relay-install.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
ASSET="relay-panel-$VERSION.tar.gz"
BASE="https://github.com/$REPO/releases/download/$VERSION"

info "Downloading Relay $VERSION"
curl -fSL --retry 3 -o "$WORK/$ASSET" "$BASE/$ASSET" || die "Download failed: $BASE/$ASSET"
curl -fsSL --retry 3 -o "$WORK/$ASSET.sha256" "$BASE/$ASSET.sha256" || die "Checksum download failed."
EXPECTED="$(cut -d' ' -f1 "$WORK/$ASSET.sha256")"
printf '%s' "$EXPECTED" | grep -Eq '^[0-9a-f]{64}$' || die "The published checksum file is not valid."
(cd "$WORK" && echo "$EXPECTED  $ASSET" | sha256sum -c --quiet -) || die "Checksum mismatch. The download is damaged; nothing was changed."
green "Checksum OK ($EXPECTED)"

mkdir "$WORK/source"
tar -xzf "$WORK/$ASSET" -C "$WORK/source" --no-same-owner --no-same-permissions
[ -f "$WORK/source/deploy/ops.py" ] || die "The release archive is incomplete."

if [ -f "$CODE_DIR/.installed" ]; then
  CURRENT="$(cat "$CODE_DIR/VERSION" 2>/dev/null || echo 'before 1.0.0')"
  info "Relay $CURRENT is installed. Updating to $VERSION (a backup is made first)."
  python3 "$WORK/source/deploy/ops.py" update --archive "$WORK/$ASSET" --sha256 "$EXPECTED"
  green "Relay updated to $VERSION. Your data and settings were kept."
  echo "See what changed: https://github.com/$REPO/blob/$VERSION/CHANGELOG.md"
else
  info "Installing Relay $VERSION"
  # install.sh copies everything it needs to /opt/3xui-reseller before this folder is removed.
  bash "$WORK/source/install.sh" "${PASS[@]+"${PASS[@]}"}"
fi
