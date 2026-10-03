#!/bin/sh
# Install ka9q-tune. Nothing here reboots or changes kernel parameters; that
# only happens when you run `ka9q-tune stage` and then reboot, or when the
# boot-time one-shot finds a staged configuration that is not applied.
set -eu

PREFIX="${PREFIX:-/usr/local}"
UNITDIR="${UNITDIR:-/etc/systemd/system}"
LIBDIR="${LIBDIR:-$PREFIX/lib/ka9q-tune}"
STATEDIR="${STATEDIR:-/var/lib/ka9q-tune}"
HERE=$(cd "$(dirname "$0")" && pwd)

echo "installing the package to $LIBDIR"
mkdir -p "$LIBDIR" "$STATEDIR" "$PREFIX/bin"
cp -r "$HERE/ka9q_tune" "$LIBDIR/"

cat > "$PREFIX/bin/ka9q-tune" <<EOF
#!/usr/bin/env python3
import sys
sys.path.insert(0, "$LIBDIR")
from ka9q_tune.cli import main
sys.exit(main())
EOF
chmod +x "$PREFIX/bin/ka9q-tune"

echo "installing units to $UNITDIR"
cp "$HERE"/systemd/*.service "$HERE"/systemd/*.timer "$UNITDIR/"

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    echo
    echo "Installed. Nothing is enabled yet. To start:"
    echo
    echo "  ka9q-tune status            # see where this station stands"
    echo "  ka9q-tune explain           # what the controls do, and the traps"
    echo
    echo "Then, when you are ready to change the machine:"
    echo
    echo "  ka9q-tune stage             # write the grub.d drop-in (no reboot)"
    echo "  systemctl enable ka9q-tune-isolate.service"
    echo "  systemctl enable ka9q-tune.service"
    echo "  systemctl enable --now ka9q-tune-check.timer"
    echo
    echo "ka9q-tune-isolate applies the staged parameters at the next boot,"
    echo "rebooting at most once if they are staged but not loaded."
fi
