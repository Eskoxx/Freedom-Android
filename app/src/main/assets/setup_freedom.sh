#!/data/data/io.freedom/files/usr/bin/bash
export PREFIX=/data/data/io.freedom/files/usr
export PATH="$PREFIX/bin:$PREFIX/bin/applets:$PATH"

# Always ensure webtorrent is available (handles upgrades)
BUNDLE="$HOME/webtorrent-bundle.tar"
if [ -f "$BUNDLE" ]; then
  tar xzf "$BUNDLE" -C "$PREFIX/lib/" 2>/dev/null || tar xf "$BUNDLE" -C "$PREFIX/lib/"
  rm -f "$BUNDLE"
fi
if [ -f "$PREFIX/lib/node_modules/webtorrent-cli/bin/cmd.js" ] && [ ! -x "$PREFIX/bin/webtorrent" ]; then
  cat > "$PREFIX/bin/webtorrent" << 'WRAPPER'
#!/data/data/io.freedom/files/usr/bin/bash
exec /data/data/io.freedom/files/usr/bin/node /data/data/io.freedom/files/usr/lib/node_modules/webtorrent-cli/bin/cmd.js "$@"
WRAPPER
  chmod 755 "$PREFIX/bin/webtorrent"
fi

SETUP_MARKER="$HOME/.freedom_setup_done"
[ -f "$SETUP_MARKER" ] && exit 0

DEBS_DIR="$HOME/debs"
if [ -d "$DEBS_DIR" ]; then
    cd "$DEBS_DIR" || exit 1
    dpkg -i --abort-after 1000 --force-all --force-depends --force-conflicts --force-overwrite ./*.deb 2>&1 | tail -10
    dpkg --configure -a --abort-after 1000 --force-all 2>&1 | tail -10
fi

pip install --no-input requests beautifulsoup4 textual 2>&1 | tail -5 || true

# Extract pre-bundled webtorrent-cli (avoids npm install on device)
BUNDLE="$HOME/webtorrent-bundle.tar"
if [ -f "$BUNDLE" ]; then
  tar xzf "$BUNDLE" -C "$PREFIX/lib/" 2>/dev/null || tar xf "$BUNDLE" -C "$PREFIX/lib/"
  rm -f "$BUNDLE"
fi
if [ -f "$PREFIX/lib/node_modules/webtorrent-cli/bin/cmd.js" ]; then
  cat > "$PREFIX/bin/webtorrent" << 'WRAPPER'
#!/data/data/io.freedom/files/usr/bin/bash
exec /data/data/io.freedom/files/usr/bin/node /data/data/io.freedom/files/usr/lib/node_modules/webtorrent-cli/bin/cmd.js "$@"
WRAPPER
  chmod 755 "$PREFIX/bin/webtorrent"
fi

touch "$SETUP_MARKER"
# Launch the TUI
if [ -d "$HOME/anime_watch" ]; then
    cd "$HOME" || exit 0
    python3 -m anime_watch
fi
