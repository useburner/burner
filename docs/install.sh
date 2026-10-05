#!/usr/bin/env bash
# burner web installer.
#   curl -fsSL https://useburner.si/install.sh | bash
#
# Fetches burner into $BURNER_DIR (default: $HOME/burner) and runs its local
# install.sh. If the directory is already a git checkout, it is updated with
# `git pull --ff-only` instead; if it was created by this installer (marker
# file .burner-web-install), it is refreshed in place. config.env, .venv/ and
# other local state are never deleted. No sudo. Safe to re-run.
set -u

# BURNER_REF picks a branch (default master). The fast branch installs with:
#   curl -fsSL https://raw.githubusercontent.com/useburner/burner/fast/docs/install.sh | BURNER_REF=fast bash
REF="${BURNER_REF:-master}"
case "$REF" in
  ""|*[!A-Za-z0-9._/-]*) echo "burner: ERROR: BURNER_REF must be a branch name (letters, digits, . _ / -)" >&2; exit 1 ;;
esac
TARBALL_URL="https://github.com/useburner/burner/archive/refs/heads/${REF}.tar.gz"
DEST="${BURNER_DIR:-$HOME/burner}"
MARKER=".burner-web-install"

die() { echo "burner: ERROR: $*" >&2; exit 1; }
info() { echo "burner: $*"; }

info "works with Muse and Android phones for now. iPhone support is coming later."

[ -n "${HOME:-}" ] || [ -n "${BURNER_DIR:-}" ] || die "neither HOME nor BURNER_DIR is set."
command -v tar >/dev/null 2>&1 || die "tar not found. Install tar and re-run."

fetch() {
  # fetch URL OUTFILE
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    die "need curl or wget to download burner."
  fi
}

if [ -e "$DEST" ] && [ ! -d "$DEST" ]; then
  die "$DEST exists and is not a directory. Move it aside or set BURNER_DIR."
fi

if [ -d "$DEST/.git" ]; then
  command -v git >/dev/null 2>&1 || die "$DEST is a git checkout but git is not installed."
  info "updating existing checkout in $DEST (git pull --ff-only)..."
  [ "$REF" = "master" ] || info "a git checkout follows its own branch: BURNER_REF=$REF is not used (git checkout $REF there if you want it)"
  git -C "$DEST" pull --ff-only \
    || die "git pull --ff-only failed in $DEST. Resolve local changes there and re-run."
elif [ -d "$DEST" ] && [ ! -f "$DEST/$MARKER" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
  die "$DEST exists and is not a git checkout. Move it aside, or set BURNER_DIR to another path, and re-run."
else
  TMPDIR_B="$(mktemp -d 2>/dev/null || mktemp -d -t burner)" || die "cannot create a temp dir."
  trap 'rm -rf "$TMPDIR_B"' EXIT
  # The branch's archive on GitHub lags a push by minutes (a cache served
  # the build before the newest, Oct 5): the branch's head commit is looked
  # up first and that commit's own archive fetched, which is exact. The
  # branch archive stands in when the lookup fails (offline, a rate limit).
  SHA=""
  if command -v curl >/dev/null 2>&1; then
    SHA="$(curl -fsSL -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/useburner/burner/commits/${REF}" 2>/dev/null | tr -dc '0-9a-f' | head -c 40)"
  elif command -v wget >/dev/null 2>&1; then
    SHA="$(wget -q -O - --header='Accept: application/vnd.github.sha' "https://api.github.com/repos/useburner/burner/commits/${REF}" 2>/dev/null | tr -dc '0-9a-f' | head -c 40)"
  fi
  if [ "${#SHA}" -eq 40 ]; then
    TARBALL_URL="https://github.com/useburner/burner/archive/${SHA}.tar.gz"
    SRC_NAME="burner-${SHA}"
    info "downloading burner ${SHA%"${SHA#???????}"} (the head of ${REF})..."
  else
    # GitHub names the folder after the branch, with / turned into -.
    SRC_NAME="burner-$(printf '%s' "$REF" | tr / -)"
    info "downloading burner..."
  fi
  fetch "$TARBALL_URL" "$TMPDIR_B/burner.tar.gz" || die "download failed: $TARBALL_URL"
  tar -xzf "$TMPDIR_B/burner.tar.gz" -C "$TMPDIR_B" || die "could not extract the burner tarball."
  SRC="$TMPDIR_B/$SRC_NAME"
  [ -f "$SRC/install.sh" ] || die "unexpected tarball layout (no $(basename "$SRC")/install.sh)."
  mkdir -p "$DEST" || die "cannot create $DEST"
  # Copy contents (including dotfiles) into DEST.
  (cd "$SRC" && tar -cf - .) | (cd "$DEST" && tar -xf -) || die "could not copy files into $DEST"
  : > "$DEST/$MARKER" || die "cannot write $DEST/$MARKER"
  # GitHub tarballs carry their commit id; `burner version` reports it.
  python3 -c 'import sys, tarfile; print(tarfile.open(sys.argv[1]).pax_headers.get("comment", ""))' \
    "$TMPDIR_B/burner.tar.gz" > "$DEST/.burner-version" 2>/dev/null || rm -f "$DEST/.burner-version"
  # `burner update` and `burner version` follow the same branch from now on.
  if [ "$REF" = "master" ]; then rm -f "$DEST/.burner-ref"; else echo "$REF" > "$DEST/.burner-ref"; fi
  info "extracted to $DEST (branch $REF)"
fi

GUIDE="https://useburner.si/skill.md"
[ "$REF" = "master" ] || GUIDE="https://raw.githubusercontent.com/useburner/burner/${REF}/SKILL-fast.md"
[ -f "$DEST/install.sh" ] || die "$DEST/install.sh not found."
info "running $DEST/install.sh..."
bash "$DEST/install.sh" || die "$DEST/install.sh failed (see output above)."

cat <<EOF

burner is ready in $DEST

Add it to your PATH (put both lines in your shell rc to keep them):

  export BURNER_WORKSPACE="$DEST"
  export PATH="$DEST/bin:\$PATH"

Next step: pair the phone (a human, once, about 10 minutes):

  burner setup

Agent guide: $GUIDE
EOF
