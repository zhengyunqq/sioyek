#!/usr/bin/env bash
# One-click build & install of sioyek on macOS (Apple Silicon / Intel).
#
#   ./install_mac.sh              build (incremental) and install to /Applications, then launch
#   ./install_mac.sh --clean      full rebuild (also rebuilds mupdf)
#   ./install_mac.sh --no-install only build; result is in build/sioyek.app
#   ./install_mac.sh --no-launch  install but don't start sioyek afterwards
#   ./install_mac.sh --prefix DIR install into DIR instead of /Applications
#
# Prerequisites: Xcode Command Line Tools and Homebrew. qt@5 is installed automatically if missing.
# User configs in ~/.config/sioyek and ~/Library/Application Support/sioyek are never touched.

set -euo pipefail
cd "$(dirname "$0")"

APP=sioyek.app
PREFIX=/Applications
CLEAN=0
INSTALL=1
LAUNCH=1

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) CLEAN=1 ;;
        --no-install) INSTALL=0; LAUNCH=0 ;;
        --no-launch) LAUNCH=0 ;;
        --prefix) PREFIX="$2"; shift ;;
        -h|--help) sed -n 2,11p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

JOBS=$(sysctl -n hw.ncpu)

# ---------------------------------------------------------------- dependencies
step "Checking dependencies"
xcode-select -p >/dev/null 2>&1 || die "Xcode Command Line Tools missing; run: xcode-select --install"
command -v brew >/dev/null 2>&1 || die "Homebrew missing; see https://brew.sh"

if ! brew list --versions qt@5 >/dev/null 2>&1; then
    step "Installing qt@5 via Homebrew"
    brew install qt@5
fi
QT_PREFIX=$(brew --prefix qt@5)
# use Homebrew's Qt explicitly (a conda/anaconda qmake on PATH would produce a broken build)
QMAKE="$QT_PREFIX/bin/qmake"
MACDEPLOYQT="$QT_PREFIX/bin/macdeployqt"
[ -x "$QMAKE" ] && [ -x "$MACDEPLOYQT" ] || die "qmake/macdeployqt not found in $QT_PREFIX/bin"
echo "Qt: $("$QMAKE" -query QT_VERSION) ($QT_PREFIX)"

if [ ! -f mupdf/Makefile ] || [ ! -f zlib/zlib.h ]; then
    step "Fetching submodules"
    git submodule update --init --recursive
fi

# ---------------------------------------------------------------- mupdf
MUPDF_LIBS="mupdf/build/release/libmupdf.a mupdf/build/release/libmupdf-third.a mupdf/build/release/libmupdf-threads.a"
need_mupdf=$CLEAN
for lib in $MUPDF_LIBS; do [ -f "$lib" ] || need_mupdf=1; done
if [ "$need_mupdf" = 1 ]; then
    step "Building mupdf"
    [ "$CLEAN" = 1 ] && make -C mupdf clean
    make -C mupdf -j"$JOBS"
else
    echo "mupdf: up to date (use --clean to rebuild)"
fi

# ---------------------------------------------------------------- sioyek
step "Building sioyek"
# regenerate the Makefile if missing, made by another qmake, or older than the .pro file
if [ "$CLEAN" = 1 ] || [ ! -f Makefile ] || ! grep -q "$QMAKE" Makefile || [ pdf_viewer_build_config.pro -nt Makefile ]; then
    "$QMAKE" -o Makefile pdf_viewer_build_config.pro CONFIG+=non_portable
fi
[ "$CLEAN" = 1 ] && make clean >/dev/null || true
# hide the very noisy (harmless) "built for newer macOS version" linker warnings
set +e +o pipefail
make -j"$JOBS" 2>&1 | grep -v "was built for newer 'macOS' version"
make_status=${PIPESTATUS[0]}
set -e -o pipefail
[ "$make_status" = 0 ] || die "build failed"
[ -x "$APP/Contents/MacOS/sioyek" ] || die "build produced no $APP"

# ---------------------------------------------------------------- bundle
step "Assembling build/$APP"
rm -rf "build/$APP"
mkdir -p build
cp -R "$APP" build/
MACOS_DIR="build/$APP/Contents/MacOS"
rm -rf "$MACOS_DIR/shaders"
cp -R pdf_viewer/shaders "$MACOS_DIR/shaders"
cp pdf_viewer/prefs.config pdf_viewer/prefs_user.config \
   pdf_viewer/keys.config  pdf_viewer/keys_user.config "$MACOS_DIR/"
cp tutorial.pdf "$MACOS_DIR/"

# bundle the Qt frameworks so the app doesn't depend on Homebrew at runtime
"$MACDEPLOYQT" "build/$APP" -always-overwrite >/dev/null
if otool -L "$MACOS_DIR/sioyek" | grep -q "$QT_PREFIX\|/opt/homebrew/opt/qt"; then
    die "macdeployqt did not relink Qt frameworks"
fi
echo "OK: build/$APP"

[ "$INSTALL" = 1 ] || exit 0

# ---------------------------------------------------------------- install
step "Installing to $PREFIX/$APP"
DEST="$PREFIX/$APP"
if [ -d "$DEST" ]; then
    # keep any user configs that were edited inside the old bundle
    for f in prefs_user.config keys_user.config; do
        if [ -s "$DEST/Contents/MacOS/$f" ]; then
            cp "$DEST/Contents/MacOS/$f" "$MACOS_DIR/$f"
            echo "kept existing $f from installed bundle"
        fi
    done
fi

if pgrep -x sioyek >/dev/null; then
    echo "quitting running sioyek"
    osascript -e 'tell application id "info.sioyek.sioyek" to quit' >/dev/null 2>&1 || true
    sleep 1
    pkill -x sioyek 2>/dev/null || true
    sleep 1
fi

SUDO=""
[ -w "$PREFIX" ] || SUDO="sudo"
$SUDO rm -rf "$DEST"
$SUDO ditto "build/$APP" "$DEST"
echo "installed: $DEST"

if [ "$LAUNCH" = 1 ]; then
    step "Launching"
    open -a "$DEST"
    sleep 2
    pgrep -x sioyek >/dev/null && echo "sioyek is running" || die "sioyek failed to start; try: $DEST/Contents/MacOS/sioyek"
fi
