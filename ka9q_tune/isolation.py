"""R1-R3: what isolation was asked for, what was delivered, and closing the gap.

R1  Report delivered state, never configured state. The kernel command line is
    an intention; /sys/devices/system/cpu/nohz_full is a fact; the local timer
    interrupt rate on radiod's core is the ground truth.

R2  Distinguish staged from applied. Writing a grub.d drop-in changes nothing
    until the kernel is reloaded, and a drop-in written after the running
    kernel booted is staged, not applied, however correct it looks.

R3  Reboot at most once, and write the marker before the reboot.
"""

import json
import os
import re
import subprocess

from . import cpuset, procfs

PARAMS = ("isolcpus", "nohz_full", "rcu_nocbs")

# Exit codes of the boot-time one-shot. Part of the interface: unit files and
# monitoring depend on them, so they are not renumbered.
EXIT_NOTHING_TO_DO = 0
EXIT_REBOOT_INVOKED = 10
EXIT_STAGED_NOT_ACTIVE = 20


def _isolcpus_cpus(value):
    """CPUs named by an isolcpus= value, ignoring its flag prefix.

    Modern kernels accept `isolcpus=managed_irq,domain,8-9`; the leading words
    are flags, not CPU numbers, and parsing them as numbers would silently
    yield an empty set and report the machine as unisolated.
    """
    if not value:
        return frozenset()
    parts = value.split(",")
    while parts and not re.fullmatch(r"\d+(-\d+)?", parts[0]):
        parts.pop(0)
    return cpuset.parse(",".join(parts))


def params_from_cmdline(text):
    """The three isolation parameters, as sets, from a kernel command line."""
    found = procfs.cmdline_params(text)
    return {
        "isolcpus": _isolcpus_cpus(found.get("isolcpus")),
        "nohz_full": cpuset.parse(found.get("nohz_full")),
        "rcu_nocbs": cpuset.parse(found.get("rcu_nocbs")),
    }


# -- staged (the grub.d drop-in) -----------------------------------------

_ASSIGN = re.compile(
    r"""^\s*(?:export\s+)?(GRUB_CMDLINE_LINUX(?:_DEFAULT)?)\s*=\s*(.*)$""", re.M
)


def read_staged(env):
    """Parse the isolation parameters staged in the grub.d drop-in.

    Returns (params, raw_text). params is empty if the drop-in is missing --
    a deleted drop-in is one of the ways isolation can never take effect, and
    R3 requires that to be reported rather than retried.
    """
    path = env.path("ISOL_CFG")
    raw = env.read(path)
    if raw is None:
        return {k: frozenset() for k in PARAMS}, None
    collected = []
    for _, value in _ASSIGN.findall(raw):
        value = value.strip()
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        # Drop-ins conventionally re-interpolate the previous value.
        value = re.sub(r"\$\{?GRUB_CMDLINE_LINUX(_DEFAULT)?\}?", " ", value)
        collected.append(value)
    return params_from_cmdline(" ".join(collected)), raw


def staged_after_boot(env):
    """True when the drop-in was modified after the running kernel booted.

    The station that lost sixteen hours had a correct drop-in, a correctly
    rebuilt grub.cfg, and a kernel booted 41 minutes before the drop-in was
    written. Nothing in the configuration was wrong; it simply had not been
    loaded. Returns None when either timestamp is unavailable.
    """
    mtime = env.mtime(env.path("ISOL_CFG"))
    booted = procfs.boot_time(env)
    if mtime is None or booted is None:
        return None
    return mtime > booted


# -- delivered (sysfs and the kernel's own threads) ----------------------

def read_delivered(env):
    """The isolation the running kernel actually applied.

    nohz_full and isolcpus have sysfs files. rcu_nocbs does not, so it is read
    from the rcuo kthreads the kernel creates per offloaded CPU.
    """
    nohz = env.read_stripped(env.sys_cpu("nohz_full"), None)
    isolated = env.read_stripped(env.sys_cpu("isolated"), None)
    return {
        "nohz_full": cpuset.parse(nohz),
        "isolcpus": cpuset.parse(isolated),
        "rcu_nocbs": procfs.nocb_cpus(env),
    }


def kernel_supports_nohz_full(env):
    """False when the kernel was built without CONFIG_NO_HZ_FULL.

    Such a kernel will never honour the parameter however many times the
    machine is rebooted, so this is a stop condition, not a retry condition.
    """
    return env.exists(env.sys_cpu("nohz_full"))


class State:
    """Everything known about isolation on this machine, in one object."""

    def __init__(self, env):
        self.env = env
        self.cmdline = procfs.kernel_cmdline(env)
        self.requested = params_from_cmdline(self.cmdline)
        self.delivered = read_delivered(env)
        self.staged, self.staged_raw = read_staged(env)
        self.staged_is_newer_than_boot = staged_after_boot(env)
        self.supported = kernel_supports_nohz_full(env)

    # The boot CPU can never be nohz_full: the kernel drops it silently, so a
    # command line reading nohz_full=0-9 yields a sysfs file reading 1-9.
    @property
    def dropped_by_kernel(self):
        """CPUs asked for on the command line that the kernel did not deliver."""
        return self.requested["nohz_full"] - self.delivered["nohz_full"]

    @property
    def staged_anything(self):
        return any(self.staged[p] for p in PARAMS)

    def staged_is_active(self):
        """True when every staged parameter is delivered by the running kernel.

        Compared as sets: `12-13` from the drop-in and `12,13` from a
        hand-written command line are the same machine state.
        """
        for param in PARAMS:
            want = self.staged[param]
            if not want:
                continue
            if not want.issubset(self.delivered[param]):
                return False
        return True

    def gap(self):
        """Per-parameter sets that are staged but not delivered."""
        return {p: self.staged[p] - self.delivered[p] for p in PARAMS}

    def effective_isolated(self):
        """CPUs that have all three mechanisms delivered.

        They only work as a set: isolcpus without nohz_full leaves the tick
        running, nohz_full without isolcpus cannot stop it whenever a second
        task is runnable, and without rcu_nocbs pending callbacks force the
        tick back on regardless. So the useful figure is the intersection.
        """
        return (
            self.delivered["isolcpus"]
            & self.delivered["nohz_full"]
            & self.delivered["rcu_nocbs"]
        )


# -- R3: the boot-time one-shot ------------------------------------------

class OneShotResult:
    def __init__(self, code, status, messages):
        self.code = code
        self.status = status
        self.messages = messages

    def __repr__(self):
        return "OneShotResult(%d, %r)" % (self.code, self.status)


def read_marker(env):
    raw = env.read(env.path("ISOL_MARKER"))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # A marker we cannot parse still forbids a second reboot. Treating an
        # unreadable marker as absent is how a reboot loop starts.
        return {"staged": None, "unparsed": raw}


def write_marker(env, staged):
    path = env.path("ISOL_MARKER")
    parent = os.path.dirname(path)
    if parent and not env.dry_run:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    payload = json.dumps(
        {
            "staged": {p: cpuset.format(staged[p]) for p in PARAMS},
            "written_at": env.now(),
            "cmdline_at_write": procfs.kernel_cmdline(env),
        },
        sort_keys=True,
    )
    return env.write(path, payload + "\n")


def clear_marker(env):
    path = env.path("ISOL_MARKER")
    if env.dry_run or not env.exists(path):
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _marker_matches(marker, staged):
    """True when the marker was written for this same staged configuration.

    A marker written for an older intention must not block a reboot for a new
    one, or an operator who fixes the drop-in finds the fix never applied.
    """
    if not marker or not isinstance(marker.get("staged"), dict):
        return True  # unparseable or unlabelled: treat as spent, never loop
    for param in PARAMS:
        if cpuset.parse(marker["staged"].get(param, "")) != staged[param]:
            return False
    return True


def grub_cfg_has(env, staged):
    """True when the generated grub.cfg already carries the staged parameters.

    A drop-in that was written but never passed through update-grub produces a
    grub.cfg that does not mention it, and rebooting achieves nothing.
    """
    raw = env.read(env.path("GRUB_CFG"))
    if raw is None:
        return None
    for param in PARAMS:
        if not staged[param]:
            continue
        for match in re.finditer(re.escape(param) + r"=(\S+)", raw):
            value = match.group(1)
            found = _isolcpus_cpus(value) if param == "isolcpus" else cpuset.parse(value)
            if staged[param].issubset(found):
                break
        else:
            return False
    return True


def run_command(env, command):
    if env.dry_run:
        return 0, "dry-run: would run %s" % command
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=600
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def one_shot(env, state=None):
    """Close the staged/applied gap, at most once per staged configuration.

    Exit codes:
      0   nothing to do -- already active, or nothing staged
      10  reboot invoked
      20  staged but NOT active and the one reboot is spent
    """
    state = state or State(env)
    messages = []

    if not state.staged_anything:
        return OneShotResult(EXIT_NOTHING_TO_DO, "nothing-staged",
                             ["no isolation staged in %s" % env.path("ISOL_CFG")])

    if state.staged_is_active():
        # Clearing here is what makes R2's "every boot, not once at install"
        # work: the one-shot is rearmed by a boot that succeeded, so a later
        # regression gets its own single reboot instead of being locked out.
        clear_marker(env)
        return OneShotResult(
            EXIT_NOTHING_TO_DO,
            "active",
            ["isolation active: " + " ".join(
                "%s=%s" % (p, cpuset.format(state.delivered[p])) for p in PARAMS
            )],
        )

    gap = state.gap()
    messages.append(
        "staged but not delivered: "
        + " ".join("%s=%s" % (p, cpuset.format(v)) for p, v in gap.items() if v)
    )

    # Stop conditions: things no number of reboots can fix. Reporting these as
    # an explicit stuck state is the whole point of R3 -- a reboot-until-it-
    # works loop buries the cause and leaves the station unreachable between
    # attempts.
    if not state.supported:
        messages.append(
            "kernel has no %s: built without CONFIG_NO_HZ_FULL; rebooting cannot help"
            % env.sys_cpu("nohz_full")
        )
        return OneShotResult(EXIT_STAGED_NOT_ACTIVE, "unsupported-kernel", messages)

    in_grub = grub_cfg_has(env, state.staged)
    if in_grub is False:
        code, output = run_command(env, env.command("GRUB_UPDATE"))
        messages.append("%s exited %d%s" % (env.command("GRUB_UPDATE"), code,
                                            (": " + output) if output else ""))
        if code != 0:
            return OneShotResult(EXIT_STAGED_NOT_ACTIVE, "grub-update-failed", messages)
        if grub_cfg_has(env, state.staged) is False:
            messages.append(
                "%s still does not carry the staged parameters after %s"
                % (env.path("GRUB_CFG"), env.command("GRUB_UPDATE"))
            )
            return OneShotResult(EXIT_STAGED_NOT_ACTIVE, "grub-cfg-stale", messages)
    elif in_grub is None:
        messages.append("could not read %s; proceeding on the drop-in alone"
                        % env.path("GRUB_CFG"))

    marker = read_marker(env)
    if marker is not None and _marker_matches(marker, state.staged):
        messages.append(
            "the one reboot for this configuration is spent (marker %s); "
            "not rebooting again" % env.path("ISOL_MARKER")
        )
        return OneShotResult(EXIT_STAGED_NOT_ACTIVE, "reboot-spent", messages)
    if marker is not None:
        messages.append("marker was written for a different configuration; "
                        "this staging gets its own reboot")

    # The marker goes down BEFORE the reboot, never after. A process killed
    # between the reboot call and the marker write would otherwise come back
    # and reboot again, and again.
    ok, detail = write_marker(env, state.staged)
    messages.append(detail)
    if not ok:
        messages.append("refusing to reboot without a marker: a failed marker "
                        "write turns one reboot into a loop")
        return OneShotResult(EXIT_STAGED_NOT_ACTIVE, "marker-write-failed", messages)

    code, output = run_command(env, env.command("REBOOT"))
    messages.append("reboot command %r exited %d%s"
                    % (env.command("REBOOT"), code, (": " + output) if output else ""))
    if code != 0:
        return OneShotResult(EXIT_STAGED_NOT_ACTIVE, "reboot-failed", messages)
    return OneShotResult(EXIT_REBOOT_INVOKED, "reboot-invoked", messages)
