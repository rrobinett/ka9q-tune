"""Every external path and command, resolved from the environment.

This is code that reboots machines and rewrites kernel parameters. It must be
fully testable without a machine, so nothing below hard-codes a path at the
point of use: everything comes through an Env instance.

Two layers of override:

  KA9Q_TUNE_ROOT    prefix applied to the default /proc, /sys and /etc paths.
                    Point it at a fixture directory and the whole program reads
                    a fake machine.

  KA9Q_TUNE_<NAME>  absolute override for one specific path or command, used
                    verbatim and never prefixed by the root.

The four SIGMOND_ISOL_* names from the precedent tool are accepted as aliases
so an existing deployment's unit files keep working.
"""

import os
import time

# name -> (default path relative to /, is_rooted)
_PATHS = {
    "PROC": ("/proc", True),
    "SYS": ("/sys", True),
    "PROC_CMDLINE": ("/proc/cmdline", True),
    "PROC_INTERRUPTS": ("/proc/interrupts", True),
    "PROC_STAT": ("/proc/stat", True),
    "RESCTRL": ("/sys/fs/resctrl", True),
    "ISOL_CFG": ("/etc/default/grub.d/99-ka9q-isolation.cfg", True),
    "GRUB_CFG": ("/boot/grub/grub.cfg", True),
    "ISOL_MARKER": ("/var/lib/ka9q-tune/isolation-reboot.marker", True),
    "STATE_DIR": ("/var/lib/ka9q-tune", True),
    "BASELINE": ("/var/lib/ka9q-tune/baseline.json", True),
    "FFT_LOG": ("/var/lib/ka9q-radio/fft.log", True),
    "WISDOM": ("/var/lib/ka9q-radio/wisdom", True),
    "CONFIG": ("/etc/ka9q-tune.conf", True),
}

_COMMANDS = {
    "REBOOT": "systemctl reboot",
    "GRUB_UPDATE": "update-grub",
    "FFTW_WISDOM": "fftwf-wisdom",
    "FFT_GEN": "fft-gen",
    "RADIOD_RESTART": "systemctl restart radiod@*",
}

_ALIASES = {
    "ISOL_CFG": "SIGMOND_ISOL_CFG",
    "ISOL_MARKER": "SIGMOND_ISOL_MARKER",
    "PROC_CMDLINE": "SIGMOND_ISOL_CMDLINE",
    "REBOOT": "SIGMOND_ISOL_REBOOT",
}


class Env:
    """Resolves every file path and external command this package touches."""

    def __init__(self, environ=None, sleep=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.root = self.environ.get("KA9Q_TUNE_ROOT", "").rstrip("/")
        # Every sampling window waits through this. Injected so a test can
        # advance a fixture machine between the two snapshots instead of
        # actually waiting, which is the only way the rate arithmetic in
        # procfs gets exercised end to end.
        self.sleep = sleep or time.sleep

    # -- lookup -----------------------------------------------------------

    def _override(self, name):
        for key in ("KA9Q_TUNE_" + name, _ALIASES.get(name)):
            if key and self.environ.get(key):
                return self.environ[key]
        return None

    def overridden(self, name):
        """True when the environment supplies this path or command."""
        return self._override(name) is not None

    def path(self, name):
        """Absolute path for a known logical name."""
        if name not in _PATHS:
            raise KeyError("unknown path name: %s" % name)
        override = self._override(name)
        if override:
            return override
        default, rooted = _PATHS[name]
        return (self.root + default) if rooted else default

    def command(self, name):
        """Shell command string for a known logical name."""
        if name not in _COMMANDS:
            raise KeyError("unknown command name: %s" % name)
        return self._override(name) or _COMMANDS[name]

    def sys_cpu(self, *parts):
        """A path under /sys/devices/system/cpu."""
        return os.path.join(self.path("SYS"), "devices", "system", "cpu", *parts)

    def proc_pid(self, pid, *parts):
        """A path under /proc/<pid>."""
        return os.path.join(self.path("PROC"), str(pid), *parts)

    # -- tunables ---------------------------------------------------------

    def flag(self, name, default=False):
        raw = self.environ.get("KA9Q_TUNE_" + name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def number(self, name, default):
        raw = self.environ.get("KA9Q_TUNE_" + name)
        if raw is None:
            return default
        try:
            return type(default)(raw)
        except (TypeError, ValueError):
            return default

    def text(self, name, default):
        return self.environ.get("KA9Q_TUNE_" + name, default)

    @property
    def dry_run(self):
        return self.flag("DRY_RUN")

    @property
    def clock_ticks(self):
        """CONFIG_USER_HZ, i.e. the units of utime/stime in /proc/<pid>/stat."""
        return self.number("CLK_TCK", os.sysconf("SC_CLK_TCK"))

    def now(self):
        """Wall-clock epoch seconds, injectable so time-dependent paths test."""
        return self.number("NOW", 0.0) or time.time()

    # -- small IO helpers -------------------------------------------------

    def read(self, path, default=None):
        try:
            with open(path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return default

    def read_stripped(self, path, default=None):
        raw = self.read(path)
        return default if raw is None else raw.strip()

    def read_int(self, path, default=None):
        raw = self.read_stripped(path)
        if raw is None:
            return default
        try:
            return int(raw.split()[0])
        except (ValueError, IndexError):
            return default

    def write(self, path, text):
        """Write, honouring dry-run. Returns (ok, message)."""
        if self.dry_run:
            return True, "dry-run: would write %r to %s" % (text, path)
        try:
            with open(path, "w") as fh:
                fh.write(text)
            return True, "wrote %s" % path
        except OSError as exc:
            return False, "could not write %s: %s" % (path, exc)

    def mtime(self, path):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def exists(self, path):
        return os.path.exists(path)

    def listdir(self, path):
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []
