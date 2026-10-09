"""Readings taken from /proc. Snapshots are pure values; rates are differences.

Keeping snapshot and rate apart is what makes the time-dependent parts of this
package testable: a test hands two snapshots and an interval to the same code
the live sampler uses, and no clock is involved.
"""

import os
import re


class Interrupts:
    """A parse of /proc/interrupts: per-IRQ, per-CPU cumulative counts."""

    def __init__(self, cpus, counts, labels):
        self.cpus = cpus            # ordered list of CPU numbers, from the header
        self.counts = counts        # irq key -> {cpu: count}
        self.labels = labels        # irq key -> trailing description

    @classmethod
    def parse(cls, text):
        cpus, counts, labels = [], {}, {}
        if not text:
            return cls(cpus, counts, labels)
        lines = text.splitlines()
        for token in lines[0].split():
            m = re.fullmatch(r"CPU(\d+)", token)
            if m:
                cpus.append(int(m.group(1)))
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, _, rest = line.partition(":")
            key = key.strip()
            if not key:
                continue
            fields = rest.split()
            per_cpu = {}
            idx = 0
            for idx, field in enumerate(fields):
                if idx >= len(cpus):
                    break
                if not field.isdigit():
                    break
                per_cpu[cpus[idx]] = int(field)
            counts[key] = per_cpu
            labels[key] = " ".join(fields[len(per_cpu):]).strip()
        return cls(cpus, counts, labels)

    def total(self, key):
        return sum(self.counts.get(key, {}).values())


def interrupt_rates(before, after, seconds):
    """Per-IRQ, per-CPU interrupts per second between two snapshots.

    Returns {irq_key: {cpu: rate}}. A counter that went backwards (CPU offlined
    and back, or a counter reset) is reported as 0 rather than a negative rate.
    """
    if seconds <= 0:
        return {}
    out = {}
    for key, after_counts in after.counts.items():
        before_counts = before.counts.get(key, {})
        per_cpu = {}
        for cpu, value in after_counts.items():
            delta = value - before_counts.get(cpu, value)
            per_cpu[cpu] = max(0.0, delta / seconds)
        out[key] = per_cpu
    return out


def tick_rates(before, after, seconds):
    """Local timer interrupts per second, per CPU. The ground truth for R1.

    The kernel command line is an intention and the sysfs file is a fact, but
    this is the thing that actually matters: whether the tick is firing.
    """
    rates = interrupt_rates(before, after, seconds)
    return rates.get("LOC", {})


# -- per-thread CPU time --------------------------------------------------

class ThreadSample:
    """utime+stime for every thread of a process, in clock ticks."""

    def __init__(self, ticks, names):
        self.ticks = ticks      # tid -> cumulative ticks
        self.names = names      # tid -> comm

    @classmethod
    def read(cls, env, pid):
        ticks, names = {}, {}
        taskdir = env.proc_pid(pid, "task")
        for entry in env.listdir(taskdir):
            if not entry.isdigit():
                continue
            tid = int(entry)
            stat = env.read(os.path.join(taskdir, entry, "stat"))
            if not stat:
                continue
            parsed = parse_stat(stat)
            if parsed is None:
                continue
            comm, utime, stime = parsed
            ticks[tid] = utime + stime
            name = env.read_stripped(os.path.join(taskdir, entry, "comm"))
            names[tid] = name or comm
        return cls(ticks, names)


def parse_stat(text):
    """Parse /proc/<pid>/task/<tid>/stat into (comm, utime, stime).

    The comm field is in parentheses and may itself contain spaces and
    parentheses, so the fields after it are located from the LAST ')' rather
    than by splitting the line.
    """
    close = text.rfind(")")
    open_paren = text.find("(")
    if close < 0 or open_paren < 0 or close < open_paren:
        return None
    comm = text[open_paren + 1:close]
    rest = text[close + 1:].split()
    # rest[0] is field 3 (state); utime is field 14, stime field 15.
    if len(rest) < 13:
        return None
    try:
        return comm, int(rest[11]), int(rest[12])
    except ValueError:
        return None


def thread_percentages(before, after, seconds, clock_ticks):
    """Per-thread CPU as a percentage of one core, between two samples."""
    if seconds <= 0 or clock_ticks <= 0:
        return {}
    out = {}
    for tid, after_ticks in after.ticks.items():
        if tid not in before.ticks:
            continue
        delta = after_ticks - before.ticks[tid]
        if delta < 0:
            continue
        out[tid] = 100.0 * (delta / clock_ticks) / seconds
    return out


# -- miscellaneous --------------------------------------------------------

def boot_time(env):
    """Epoch seconds at which the running kernel booted, from /proc/stat btime.

    `uptime -s` reports the same instant; reading btime avoids shelling out and
    avoids a locale-dependent date string.
    """
    text = env.read(env.path("PROC_STAT"), "")
    for line in (text or "").splitlines():
        if line.startswith("btime"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
    return None


def process_start(env, pid):
    """Epoch seconds at which a process started: btime plus stat field 22."""
    btime = boot_time(env)
    stat = env.read(env.proc_pid(pid, "stat"), "") or ""
    close = stat.rfind(")")
    rest = stat[close + 1:].split() if close >= 0 else []
    # rest[0] is field 3, so field 22 (starttime, in clock ticks) is rest[19].
    if btime is None or len(rest) < 20 or env.clock_ticks <= 0:
        return None
    try:
        return btime + int(rest[19]) / env.clock_ticks
    except ValueError:
        return None


def kernel_cmdline(env):
    return env.read_stripped(env.path("PROC_CMDLINE"), "") or ""


def cmdline_params(text):
    """Split a kernel command line into {key: value}, last occurrence winning.

    Bare flags map to an empty string. Quoted values are unquoted.
    """
    out = {}
    for token in (text or "").split():
        key, sep, value = token.partition("=")
        if sep:
            out[key] = value.strip('"')
        else:
            out[key] = ""
    return out


def find_pid(env, pattern):
    """Lowest PID actually running `pattern`. Overridable by env.

    Matched on the thread name and on argv[0]'s basename, NOT on a substring
    of the whole command line: anything that merely mentions radiod -- a shell
    running a script about it, an editor, this tool's own invocation -- would
    otherwise be picked up and every reading below would describe the wrong
    process while looking perfectly plausible.
    """
    override = env.environ.get("KA9Q_TUNE_RADIOD_PID")
    if override:
        try:
            return int(override)
        except ValueError:
            return None
    procdir = env.path("PROC")
    best = None
    for entry in env.listdir(procdir):
        if not entry.isdigit():
            continue
        comm = env.read_stripped(os.path.join(procdir, entry, "comm"), "") or ""
        argv = (env.read(os.path.join(procdir, entry, "cmdline"), "") or "").split("\0")
        argv0 = os.path.basename(argv[0]) if argv and argv[0] else ""
        if comm == pattern or argv0 == pattern:
            pid = int(entry)
            if best is None or pid < best:
                best = pid
    return best


def process_cmdline(env, pid):
    raw = env.read(env.proc_pid(pid, "cmdline"), "") or ""
    return " ".join(raw.replace("\0", " ").split())


def nocb_cpus(env):
    """CPUs whose RCU callbacks are offloaded, as delivered.

    There is no sysfs file for rcu_nocbs, but the kernel creates an `rcuop/N`
    kthread per offloaded CPU. Their presence is a fact about the running
    kernel; the command line is only a request.
    """
    found = set()
    procdir = env.path("PROC")
    for entry in env.listdir(procdir):
        if not entry.isdigit():
            continue
        comm = env.read_stripped(os.path.join(procdir, entry, "comm"), "") or ""
        m = re.fullmatch(r"rcuo[pb]?/(\d+)", comm)
        if m:
            found.add(int(m.group(1)))
    return frozenset(found)
