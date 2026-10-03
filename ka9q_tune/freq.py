"""R7: pin frequency with min == max, then verify what was delivered.

A max-only cap lets amd-pstate-epp park an isolated nohz_full CPU at
scaling_min_freq, because with the tick suppressed the governor never sees the
load that would make it scale up. Both ends have to be set.

And then the result has to be read back from something other than the thing
that was written: scaling_cur_freq can echo the request on several drivers.
cpuinfo_cur_freq is a hardware read and is preferred wherever it exists; where
it does not, this reports the figure as unverified rather than claiming it.
"""

import os


class CpuFreq:
    def __init__(self, env, cpu):
        self.env = env
        self.cpu = cpu
        self.base = env.sys_cpu("cpu%d" % cpu, "cpufreq")

    def _int(self, name):
        return self.env.read_int(os.path.join(self.base, name))

    def _str(self, name):
        return self.env.read_stripped(os.path.join(self.base, name), None)

    @property
    def present(self):
        return self.env.exists(self.base)

    @property
    def min_khz(self):
        return self._int("scaling_min_freq")

    @property
    def max_khz(self):
        return self._int("scaling_max_freq")

    @property
    def hw_min_khz(self):
        return self._int("cpuinfo_min_freq")

    @property
    def hw_max_khz(self):
        return self._int("cpuinfo_max_freq")

    @property
    def driver(self):
        return self._str("scaling_driver")

    @property
    def governor(self):
        return self._str("scaling_governor")

    @property
    def epp(self):
        return self._str("energy_performance_preference")

    @property
    def pinned(self):
        """True when min == max, which is what R7 asks for."""
        lo, hi = self.min_khz, self.max_khz
        return lo is not None and hi is not None and lo == hi

    def delivered(self):
        """(khz, verified) -- the frequency the CPU is actually running at.

        verified is False when the only figure available is scaling_cur_freq,
        which on some drivers returns the setpoint rather than a measurement.
        """
        hw = self._int("cpuinfo_cur_freq")
        if hw is not None:
            return hw, True
        return self._int("scaling_cur_freq"), False

    def set_pinned(self, khz):
        """Write min == max == khz. Returns (ok, [messages]).

        Ordered so no intermediate state is rejected: drop the floor, raise the
        ceiling, then raise the floor. Writing min above the current max fails,
        and a half-applied pin is the failure mode R7 exists to prevent.
        """
        messages = []
        if not self.present:
            return False, ["cpu%d has no cpufreq interface" % self.cpu]
        floor = self.hw_min_khz
        for name, value in (
            ("scaling_min_freq", floor if floor is not None else khz),
            ("scaling_max_freq", khz),
            ("scaling_min_freq", khz),
        ):
            ok, detail = self.env.write(os.path.join(self.base, name), "%d\n" % value)
            if not ok:
                return False, messages + ["cpu%d %s: %s" % (self.cpu, name, detail)]
        if self.env.dry_run:
            return True, ["dry-run: would pin cpu%d to %d kHz" % (self.cpu, khz)]
        # Read back the setpoints, and separately the delivered frequency.
        if not self.pinned:
            return False, messages + [
                "cpu%d: min=%s max=%s after the write; not pinned"
                % (self.cpu, self.min_khz, self.max_khz)
            ]
        got, verified = self.delivered()
        messages.append(
            "cpu%d pinned to %d MHz; delivered %s%s"
            % (self.cpu, khz // 1000,
               ("%d MHz" % (got // 1000)) if got else "unknown",
               "" if verified else " (scaling_cur_freq, may echo the setpoint)")
        )
        return True, messages


def survey(env, cpus):
    return {cpu: CpuFreq(env, cpu) for cpu in sorted(cpus)}


def target_khz(env, cpus):
    """The frequency to pin to: the configured one, else hardware maximum."""
    explicit = env.number("FREQ_KHZ", 0)
    if explicit:
        return int(explicit)
    mhz = env.number("FREQ_MHZ", 0)
    if mhz:
        return int(mhz) * 1000
    maxima = [CpuFreq(env, c).hw_max_khz for c in cpus]
    maxima = [m for m in maxima if m]
    return min(maxima) if maxima else None
