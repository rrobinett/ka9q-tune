"""Collect every reading in one window, then render the operator's report.

Constraint from the specification, applied throughout this module: never
report success from configuration, and never fail silently. Where a reading
could not be taken, the report says so in the line where the reading belongs
rather than omitting the line.
"""

import time

from . import cache, cpuset, diagnose, fftw, freq as freq_mod, irq as irq_mod
from . import isolation, procfs, radiod as radiod_mod, topology as topo_mod

OK = "ok"
WARN = "warn"
BAD = "bad"
UNKNOWN = "unknown"

_RANK = {OK: 0, UNKNOWN: 1, WARN: 2, BAD: 3}


class Line:
    def __init__(self, label, value, state=OK, notes=None):
        self.label = label
        self.value = value
        self.state = state
        self.notes = notes or []


class Status:
    def __init__(self):
        self.lines = []
        self.verdict = None
        self.reading = None
        self.problems = []

    def add(self, label, value, state=OK, notes=None):
        line = Line(label, value, state, notes)
        self.lines.append(line)
        if state in (WARN, BAD):
            self.problems.append((state, label, value))
        return line

    @property
    def worst(self):
        return max((l.state for l in self.lines), key=lambda s: _RANK[s], default=OK)

    @property
    def exit_code(self):
        worst = self.worst
        return {OK: 0, UNKNOWN: 0, WARN: 1, BAD: 2}[worst]


def collect(env, seconds=30.0, sleep=None):
    """Take every reading over one sampling window."""
    sleep = sleep or env.sleep
    status = Status()
    topology = topo_mod.Topology(env)
    radiod = radiod_mod.Radiod(env, topology)
    state = isolation.State(env)

    irq_before = procfs.Interrupts.parse(env.read(env.path("PROC_INTERRUPTS"), ""))
    threads_before = procfs.ThreadSample.read(env, radiod.pid) if radiod.running else None
    sleep(seconds)
    irq_after = procfs.Interrupts.parse(env.read(env.path("PROC_INTERRUPTS"), ""))
    threads_after = procfs.ThreadSample.read(env, radiod.pid) if radiod.running else None

    ticks = procfs.tick_rates(irq_before, irq_after, seconds)
    rates = procfs.interrupt_rates(irq_before, irq_after, seconds)

    # The hot threads' CPUs, not the union over every thread: under the split
    # layout the minor threads are on housekeeping CPUs on purpose.
    cpus = radiod.hot_cpus() if radiod.running else frozenset()
    isolated = state.effective_isolated()

    _radiod_line(status, radiod, topology, cpus, isolated)
    _isolation_lines(status, env, state, cpus, isolated, ticks, topology)
    _freq_line(status, env, cpus or isolated)
    _cache_line(status, env, topology, radiod)
    _irq_line(status, env, rates, irq_after.labels, cpus or isolated, topology, isolated)
    _wisdom_line(status, env, radiod.started if radiod.running else None)

    if threads_before and threads_after:
        status.reading = diagnose.reading_from(
            threads_before, threads_after, seconds, env.clock_ticks
        )
        ref_fft, ref_ingest, source = diagnose.reference(env)
        status.verdict = diagnose.verdict(status.reading, ref_fft, ref_ingest, source)
    return status


# -- individual readings -------------------------------------------------

def _radiod_line(status, radiod, topology, cpus, isolated=frozenset()):
    if not radiod.running:
        status.add("radiod", "NOT RUNNING", BAD,
                   ["No process matched; every reading below is about an idle machine."])
        return
    placement = radiod.placement()
    layout = placement.layout
    state = OK
    notes = []
    if not cpus:
        state, where = UNKNOWN, "affinity unreadable"
    elif layout == radiod_mod.SPLIT:
        where = ("fft on cpu%d, proc_rx888 on cpu%d (split: separate physical cores)"
                 % (min(placement.fft), min(placement.ingest)))
    else:
        where = "cpus %s (%s)" % (cpuset.format(cpus), topology.describe(cpus))

    if cpus and topology.boot_cpu in cpus:
        state = BAD
        notes.append(
            "radiod is on the boot CPU (%d), which the kernel silently refuses "
            "to make nohz_full. Half its sibling pair will keep ticking no "
            "matter what the command line says." % topology.boot_cpu
        )
    elif layout == radiod_mod.SPLIT:
        busy = sorted(placement.idle_siblings() - frozenset(isolated))
        if busy:
            state = WARN
            notes.append(
                "The other logical CPU of each hot core should be isolated and "
                "idle; %s is not, so another task can share a hot thread's core."
                % cpuset.format(busy))
        if placement.others:
            notes.append("%d other radiod threads on %s"
                         % (len(radiod.threads) - len(radiod.hot_tids()[0])
                            - len(radiod.hot_tids()[1]),
                            cpuset.format(placement.others)))
    elif cpus and layout is None:
        state = WARN
        notes.append(
            "Neither layout: the hot threads share %s across separate cores "
            "without per-thread pinning, so they can land on one CPU together. "
            "Use --layout pair (one core's two hyperthreads) or --layout split "
            "(a core each); `ka9q-tune layout` measures which is faster here."
            % cpuset.format(cpus))
    status.add("radiod", "%s  pid %d   %s" % (radiod.unit, radiod.pid, where),
               state, notes)


def _isolation_lines(status, env, state, cpus, isolated, ticks, topology):
    want = cpus or isolated
    requested = " ".join(
        "%s=%s" % (p, cpuset.format(state.requested[p]) or "-")
        for p in isolation.PARAMS
    )
    delivered_sets = {p: state.delivered[p] for p in isolation.PARAMS}

    covered = bool(want) and frozenset(want) <= isolated
    if not state.supported:
        headline, line_state = "UNSUPPORTED", BAD
    elif covered:
        headline, line_state = "active", OK
    elif isolated:
        headline, line_state = "PARTIAL", BAD
    else:
        headline, line_state = "NOT ACTIVE", BAD

    notes = []
    if not state.supported:
        notes.append("%s does not exist: this kernel was built without "
                     "CONFIG_NO_HZ_FULL." % env.sys_cpu("nohz_full"))
    missing = [p for p in isolation.PARAMS
               if want and not frozenset(want) <= state.delivered[p]]
    if missing:
        notes.append(
            "radiod's cores are missing: " + ", ".join(missing)
            + ". The three only work as a set -- isolcpus keeps the second "
            "runnable task away so nohz_full can stop the tick, and rcu_nocbs "
            "stops pending callbacks forcing it back on."
        )
    status.add("isolation", "%s   %s" % (headline, requested), line_state, notes)

    status.add("  delivered",
               " ".join("%s=%s" % (p, cpuset.format(delivered_sets[p]) or "-")
                        for p in isolation.PARAMS)
               + "   (sysfs and rcuo kthreads, not /proc/cmdline)",
               OK if covered else UNKNOWN)

    dropped = state.dropped_by_kernel
    if dropped:
        status.add("  dropped by kernel", cpuset.format(dropped), BAD, [
            "The command line asked for these and the kernel did not deliver "
            "them. CPU %s is the boot CPU and can never be nohz_full; the "
            "kernel drops it silently." % cpuset.format(dropped)
        ])

    _tick_line(status, env, want, ticks)
    _staged_line(status, env, state)


def _tick_line(status, env, cpus, ticks):
    """The ground truth. A config file can lie about this; the counter cannot."""
    if not ticks:
        status.add("  tick", "unreadable (no LOC line in /proc/interrupts)", UNKNOWN)
        return
    hz = env.number("CONFIG_HZ", 0)
    targets = sorted(cpus) if cpus else sorted(ticks)
    shown = []
    worst = OK
    for cpu in targets:
        rate = ticks.get(cpu)
        if rate is None:
            continue
        shown.append("%.1f/s on cpu%d" % (rate, cpu))
        if rate >= 50.0:
            worst = BAD
        elif rate >= 5.0 and worst != BAD:
            worst = WARN
    note = []
    if worst == BAD:
        note.append(
            "This is the full periodic scheduler tick. nohz_full only stops "
            "the tick when exactly one task is runnable on the CPU; with two, "
            "the kernel needs the tick to preempt between them and brings it "
            "straight back. That is what isolcpus is for."
        )
        if hz:
            note.append("CONFIG_HZ=%d on this kernel, so ~%d/s is the untouched "
                        "tick." % (hz, hz))
    status.add("  tick", "   ".join(shown) or "no reading", worst, note)


def _staged_line(status, env, state):
    if not state.staged_anything:
        status.add("  staged", "nothing staged in %s" % env.path("ISOL_CFG"), OK)
        return
    staged = " ".join("%s=%s" % (p, cpuset.format(state.staged[p]) or "-")
                      for p in isolation.PARAMS)
    if state.staged_is_active():
        status.add("  staged", staged + "   (applied)", OK)
        return
    notes = ["A drop-in changes nothing until the kernel is reloaded."]
    if state.staged_is_newer_than_boot:
        notes.append(
            "The drop-in was modified AFTER the running kernel booted, so this "
            "configuration has never been loaded. Correct on disk, absent from "
            "the running system."
        )
    status.add("  staged", staged + "   STAGED, NOT APPLIED", BAD, notes)


def _freq_line(status, env, cpus):
    if not cpus:
        status.add("frequency", "no CPUs to check", UNKNOWN)
        return
    survey = freq_mod.survey(env, cpus)
    present = {c: f for c, f in survey.items() if f.present}
    if not present:
        status.add("frequency", "no cpufreq interface", UNKNOWN)
        return
    unpinned = [c for c, f in present.items() if not f.pinned]
    delivered, verified = [], True
    for cpu, f in sorted(present.items()):
        khz, is_verified = f.delivered()
        verified = verified and is_verified
        delivered.append("%s MHz" % (khz // 1000 if khz else "?"))
    shown = "/".join(sorted(set(delivered)))
    notes = []
    state = OK
    if not unpinned:
        value = "%s delivered, min == max" % shown
    else:
        value = "%s delivered, min != max on cpu%s" % (
            shown, ",".join(str(c) for c in sorted(unpinned)))
        state = BAD
        notes.append(
            "A max-only cap lets amd-pstate-epp park an isolated nohz_full CPU "
            "at scaling_min_freq, because with the tick suppressed the "
            "governor never sees load. Set both ends."
        )
    if not verified:
        if state == OK:
            state = WARN
        notes.append(
            "Delivered frequency came from scaling_cur_freq, which on some "
            "drivers echoes the setpoint. cpuinfo_cur_freq would be a hardware "
            "read; it is not exposed here."
        )
    drivers = sorted({f.driver for f in present.values() if f.driver})
    if drivers:
        notes.append("driver: " + ", ".join(drivers))
    status.add("frequency", value, state, notes)


def _cache_line(status, env, topology, radiod):
    resctrl = cache.Resctrl(env, topology)
    if not resctrl.available:
        status.add("L3 partition", "resctrl not available at %s" % resctrl.root,
                   UNKNOWN,
                   ["Not necessarily a problem. The measured sweep found a "
                    "cliff below ~4 MiB and a knee at ~5 MiB, past which more "
                    "cache bought nothing -- worth setting, not worth "
                    "over-tuning."])
        return
    group = env.text("L3_GROUP", cache.DEFAULT_GROUP)
    sizes = resctrl.group_size(group)
    if not sizes:
        status.add("L3 partition", "no resctrl group %r" % group, WARN,
                   ["radiod shares the whole cache with everything else on "
                    "the machine."])
        return
    bits = resctrl.num_bits()
    schemata = resctrl.group_schemata(group).get("L3", {})
    parts, state, notes = [], OK, []
    for domain, size in sorted(sizes.items()):
        total = resctrl.domain_bytes(domain)
        ways = bin(schemata.get(domain, 0)).count("1")
        parts.append("%s of %s (%d/%d ways)"
                     % (topo_mod.human_bytes(size), topo_mod.human_bytes(total),
                        ways, bits))
        if size < cache.CLIFF_BYTES:
            state = BAD
            notes.append("L3:%d is below the ~4 MiB cliff." % domain)
        elif size < cache.DEFAULT_TARGET_BYTES:
            state = WARN if state == OK else state
            notes.append("L3:%d is below the ~5 MiB knee." % domain)
    if state == OK:
        notes.append("past the knee")
    status.add("L3 partition", "%s %s" % (group, "; ".join(parts)), state, notes)


def _irq_line(status, env, rates, labels, cpus, topology, isolated):
    if not rates:
        status.add("interrupts", "unreadable", UNKNOWN)
        return
    threshold = env.number("IRQ_HIGH_RATE", irq_mod.DEFAULT_HIGH_RATE)
    findings = irq_mod.conflicts(env, rates, labels, cpus, threshold)
    if not findings:
        housekeeping = irq_mod.housekeeping_cpus(topology, isolated)
        status.add("device IRQs",
                   "none above %.0f/s on %s   (housekeeping: %s)"
                   % (threshold, cpuset.format(cpus) or "radiod's cores",
                      cpuset.format(housekeeping)),
                   OK)
        return
    worst = findings[0]
    notes = [
        "A high-rate interrupt on a nohz_full core means the CPU never stays "
        "tickless: all of nohz_full's cost, none of its benefit.",
        "Measured as gaps per channel-hour: 0.68 with the interrupt elsewhere "
        "and no isolation, 20.82 with isolation and the interrupt co-located.",
        "Move it to a housekeeping CPU (%s), or move radiod."
        % cpuset.format(irq_mod.housekeeping_cpus(topology, isolated)),
    ]
    if len(findings) > 1:
        notes.append("also: " + ", ".join(
            "%s at %.0f/s on cpu%d" % (f.key, f.rate, f.cpu)
            for f in findings[1:4]))
    status.add("device IRQs",
               "IRQ %s (%s) at %.0f/s on cpu%d"
               % (worst.key, irq_mod.irq_name(env, worst.key), worst.rate, worst.cpu),
               BAD, notes)


def _age(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return "%d min" % minutes
    return "%d h %02d min" % (minutes // 60, minutes % 60)


def _wisdom_line(status, env, started=None):
    misses, unparsed, exists = fftw.read_log(env)
    if not exists:
        status.add("fftw wisdom", "%s absent" % env.path("FFT_LOG"), UNKNOWN,
                   ["Absent is not the same as empty. radiod may not have run, "
                    "or may be logging elsewhere."])
        return
    if not misses and not unparsed:
        status.add("fftw wisdom", "fft.log empty", OK, ["no ESTIMATE plans"])
        return
    notes = []
    if misses:
        notes.append("transforms: " + " ".join(m.spec for m in misses[:12])
                     + (" ..." if len(misses) > 12 else ""))
    count = len(misses) or len(unparsed)
    stale = fftw.log_predates(env, started)
    if stale:
        # Every line is from an earlier run. Not proof this radiod is clean --
        # a transform it has not built yet can still miss -- but not evidence
        # of a miss either, and wisdom may have been planned since.
        notes += [
            "fft.log was last written %s before this radiod started, and this "
            "radiod has logged no miss. These are earlier runs' transforms; "
            "wisdom may have been planned for them since."
            % _age(started - env.mtime(env.path("FFT_LOG"))),
            "`ka9q-tune wisdom --plan` re-plans them, and is quick for any "
            "that already have wisdom.",
        ]
    else:
        notes += [
            "radiod plans FFTW_WISDOM_ONLY|FFTW_PATIENT and falls back silently "
            "to FFTW_ESTIMATE on a miss. An ESTIMATE plan is heuristic, "
            "unmeasured, and kept for the life of the process.",
            "This file IS the list of transforms on bad plans. Converge on it "
            "with `ka9q-tune wisdom --plan`; a static list of sizes can never "
            "be complete.",
        ]
    if unparsed:
        notes.append("%d line(s) did not match the transform pattern; first: %s"
                     % (len(unparsed), unparsed[0]))
    if stale:
        status.add("fftw wisdom", "%d transform(s) in a stale fft.log" % count,
                   WARN, notes)
    else:
        status.add("fftw wisdom", "%d transform(s) on ESTIMATE plans" % count,
                   BAD, notes)


# -- rendering -----------------------------------------------------------

_MARK = {OK: "ok", WARN: "WARN", BAD: "BAD", UNKNOWN: "?"}


def render(status, verbose=True, width=18):
    out = []
    for line in status.lines:
        mark = _MARK[line.state]
        label = line.label.ljust(width)
        suffix = "" if line.state == OK and not verbose else "   [%s]" % mark
        out.append("%s%s%s" % (label, line.value, suffix))
        if verbose:
            for note in line.notes:
                for chunk in _wrap(note, 76):
                    out.append(" " * (width + 2) + chunk)
    if status.reading:
        out.append("")
        fft = status.reading.fft
        ing = status.reading.ingest
        out.append("%s%s   %s"
                   % ("threads".ljust(width),
                      ("fft %s" % _pct(fft)).ljust(16),
                      "proc_rx888 %s" % _pct(ing)))
        out.append(" " * (width + 2)
                   + "both scaling together would indicate the CORE, not the plans")
    if status.verdict:
        out.append("")
        headline = _wrap(status.verdict.headline, 76)
        out.append("%s%s" % ("diagnosis".ljust(width), headline[0]))
        for chunk in headline[1:]:
            out.append(" " * width + chunk)
        if verbose:
            for note in status.verdict.detail:
                for chunk in _wrap(note, 76):
                    out.append(" " * (width + 2) + chunk)
            for chunk in _wrap("reference: " + status.verdict.reference_source, 76):
                out.append(" " * (width + 2) + chunk)
    return "\n".join(out)


def _pct(value):
    return "%.1f%%" % value if value is not None else "not found"


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word) if line else word
    if line:
        out.append(line)
    return out
