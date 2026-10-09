"""R4: find radiod, read where its threads actually run, and place them well.

Two layouts, and which is faster depends on the CPU, so neither is assumed:

  pair    fft and proc_rx888 on the two logical CPUs of ONE physical core.
          They share L1 and L2. Measured faster on 6- and 8-core mobile Ryzen.
  split   fft and proc_rx888 on two DIFFERENT physical cores, each alone, with
          the other logical CPU of both cores isolated and left idle. Measured
          faster on a Skylake-SP Xeon (dp0, 2026-10-08: fft 74% as a pair, 62%
          split), where the AVX-512 transform wants the whole core.

`ka9q-tune layout` measures both on the running radiod. Either way the CPUs
come from topology, never from CPU numbering, and never include the boot CPU.
"""

import os

from . import cpuset, procfs

# The two hot threads. proc_rx888 does no FFT, which is what makes it the
# discriminator: when it moves with the FFT thread, the cause is the core.
FFT_THREAD = "fft"
INGEST_THREAD = "proc_rx888"

UNIT_PREFIXES = ("radiod@", "ka9q-radio@")


class Radiod:
    def __init__(self, env, topology):
        self.env = env
        self.topology = topology
        self.pid = procfs.find_pid(env, env.text("RADIOD_PATTERN", "radiod"))
        self.cmdline = procfs.process_cmdline(env, self.pid) if self.pid else ""
        self.unit = self._unit()
        self.threads = self._threads()

    @property
    def started(self):
        """Epoch seconds the process started, or None."""
        return procfs.process_start(self.env, self.pid) if self.pid else None

    @property
    def running(self):
        return self.pid is not None

    def _unit(self):
        """The systemd instance running radiod: radiod@<station>, or
        ka9q-radio@<device> as the packaged install names it."""
        override = self.env.text("RADIOD_UNIT", None)
        if override:
            return override
        if not self.pid:
            return None
        cgroup = self.env.read(self.env.proc_pid(self.pid, "cgroup"), "") or ""
        for line in cgroup.splitlines():
            for field in line.split("/"):
                if (field.startswith(UNIT_PREFIXES)
                        and field.endswith(".service")):
                    return field[:-len(".service")]
        for token in self.cmdline.split():
            if token.endswith(".conf"):
                return "radiod@" + os.path.basename(token)[:-len(".conf")]
        return "radiod"

    def _threads(self):
        """tid -> name, for every thread of the process."""
        out = {}
        if not self.pid:
            return out
        taskdir = self.env.proc_pid(self.pid, "task")
        for entry in self.env.listdir(taskdir):
            if not entry.isdigit():
                continue
            name = self.env.read_stripped(os.path.join(taskdir, entry, "comm"), "")
            out[int(entry)] = name or ""
        return out

    def tids_named(self, name):
        return [tid for tid, comm in self.threads.items() if comm == name]

    def affinity(self, tid=None):
        """CPUs a thread is allowed on, from Cpus_allowed_list in /proc.

        Read rather than queried through sched_getaffinity so that the same
        code path works against a fixture tree.
        """
        if not self.pid:
            return frozenset()
        target = tid if tid is not None else self.pid
        status = self.env.read(
            os.path.join(self.env.proc_pid(self.pid, "task"), str(target), "status"), ""
        )
        if not status and tid is None:
            status = self.env.read(self.env.proc_pid(self.pid, "status"), "")
        for line in (status or "").splitlines():
            if line.startswith("Cpus_allowed_list:"):
                return cpuset.parse(line.split(":", 1)[1])
        return frozenset()

    def hot_tids(self):
        """tids of the two hot threads: (fft, proc_rx888)."""
        return self.tids_named(FFT_THREAD), self.tids_named(INGEST_THREAD)

    def hot_cpus(self):
        """The CPUs radiod's two hot threads may run on.

        This, not the union over every thread, is what isolation, the tick,
        interrupts and frequency must be judged against: under the split
        layout the minor threads sit on housekeeping CPUs on purpose, and
        counting them reports a correct station as running on the boot CPU.
        Falls back to the process affinity when the hot threads are not found.
        """
        fft, ingest = self.hot_tids()
        cpus = set()
        for tid in fft + ingest:
            cpus |= self.affinity(tid)
        return frozenset(cpus) if cpus else self.process_affinity()

    def placement(self):
        """How the hot threads are placed: a Placement, read from /proc."""
        fft, ingest = self.hot_tids()
        fft_cpus = frozenset().union(*(self.affinity(t) for t in fft)) if fft else frozenset()
        ingest_cpus = (frozenset().union(*(self.affinity(t) for t in ingest))
                       if ingest else frozenset())
        others = frozenset().union(*(
            self.affinity(t) for t in self.threads if t not in fft and t not in ingest
        )) if self.threads else frozenset()
        return Placement(self.topology, fft_cpus, ingest_cpus, others)

    def set_affinity(self, tid, cpus):
        """The one place a thread is moved. Raises OSError."""
        os.sched_setaffinity(tid, set(cpus))

    def pin_split(self, fft_cpu, ingest_cpu, others):
        """fft alone on fft_cpu, proc_rx888 alone on ingest_cpu, the rest on
        `others` (housekeeping). Returns (ok, [messages])."""
        if not self.pid:
            return False, ["radiod is not running; nothing to pin"]
        fft, ingest = self.hot_tids()
        if not fft or not ingest:
            return False, ["radiod has no fft or proc_rx888 thread to place"]
        if not others:
            return False, ["refusing to put the minor threads on an empty CPU set"]
        plan = [(t, frozenset([fft_cpu])) for t in fft]
        plan += [(t, frozenset([ingest_cpu])) for t in ingest]
        plan += [(t, frozenset(others)) for t in self.threads
                 if t not in fft and t not in ingest]
        if self.env.dry_run:
            return True, ["dry-run: would pin fft to cpu%d, proc_rx888 to cpu%d, "
                          "%d other threads to %s"
                          % (fft_cpu, ingest_cpu, len(plan) - len(fft) - len(ingest),
                             cpuset.format(others))]
        messages, failures = [], 0
        for tid, cpus in plan:
            try:
                self.set_affinity(tid, cpus)
            except OSError as exc:
                failures += 1
                messages.append("tid %d: %s" % (tid, exc))
        if failures:
            messages.insert(0, "failed to pin %d of %d threads" % (failures, len(plan)))
            return False, messages
        messages.append("pinned fft to cpu%d, proc_rx888 to cpu%d, %d other "
                        "threads to %s" % (fft_cpu, ingest_cpu,
                                           len(plan) - len(fft) - len(ingest),
                                           cpuset.format(others)))
        return True, messages

    def process_affinity(self):
        """The union of every thread's affinity, which is what an operator sees."""
        if not self.threads:
            return self.affinity()
        union = set()
        for tid in self.threads:
            union |= self.affinity(tid)
        return frozenset(union)

    def running_cpu(self, tid):
        """The CPU a thread was last seen on -- field 39 of its stat line."""
        stat = self.env.read(
            os.path.join(self.env.proc_pid(self.pid, "task"), str(tid), "stat"), ""
        )
        close = (stat or "").rfind(")")
        if close < 0:
            return None
        rest = stat[close + 1:].split()
        if len(rest) < 37:
            return None
        try:
            return int(rest[36])
        except ValueError:
            return None

    # -- placement --------------------------------------------------------

    def pin(self, cpus):
        """Set affinity for every thread. Returns (ok, [messages])."""
        cpus = set(cpus)
        messages = []
        if not self.pid:
            return False, ["radiod is not running; nothing to pin"]
        if not cpus:
            return False, ["refusing to pin to an empty CPU set"]
        if self.env.dry_run:
            return True, ["dry-run: would pin %d threads to %s"
                          % (len(self.threads) or 1, cpuset.format(cpus))]
        failures = 0
        for tid in (self.threads or {self.pid: ""}):
            try:
                self.set_affinity(tid, cpus)
            except OSError as exc:
                failures += 1
                messages.append("tid %d: %s" % (tid, exc))
        if failures:
            messages.insert(0, "failed to pin %d of %d threads"
                            % (failures, len(self.threads) or 1))
            return False, messages
        messages.append("pinned %d threads to %s"
                        % (len(self.threads) or 1, cpuset.format(cpus)))
        return True, messages


def choose_pair(topology, isolated, avoid=frozenset(), required=None):
    """Pick the sibling pair to run radiod on.

    Preference order, best first:
      - every logical CPU of the pair is fully isolated and carries no
        high-rate interrupt
      - it is not the boot CPU's pair, which can never be nohz_full
      - lower-numbered pairs, for a stable answer across runs

    `avoid` is the set of CPUs carrying high-rate interrupts (R6). Returns
    (pair, reason) with pair empty when nothing qualifies.
    """
    boot = topology.boot_cpu
    candidates = []
    for pair in topology.sibling_pairs():
        if boot in pair:
            continue
        if required is not None and not frozenset(pair) <= frozenset(required):
            continue
        fully_isolated = frozenset(pair) <= frozenset(isolated)
        clean = not (frozenset(pair) & frozenset(avoid))
        candidates.append(((not fully_isolated, not clean, min(pair)), pair))
    if not candidates:
        return frozenset(), (
            "no sibling pair qualifies: every physical core either holds the "
            "boot CPU (%d) or was excluded" % boot
        )
    candidates.sort()
    (not_isolated, not_clean, _), pair = candidates[0]
    reasons = []
    if not_isolated:
        reasons.append("not fully isolated")
    if not_clean:
        reasons.append("carries a high-rate interrupt")
    if reasons:
        return frozenset(pair), "best available, but " + " and ".join(reasons)
    return frozenset(pair), "fully isolated, no high-rate interrupt"


PAIR = "pair"
SPLIT = "split"
LAYOUTS = (PAIR, SPLIT)


class Placement:
    """Where the two hot threads are allowed to run, and what that amounts to.

    layout is PAIR when both hot threads share one physical core's sibling
    set, SPLIT when each is confined to one CPU on its own physical core, and
    None for anything else (unpinned, or sharing CPUs across cores).
    """

    def __init__(self, topology, fft_cpus, ingest_cpus, others=frozenset()):
        self.topology = topology
        self.fft = frozenset(fft_cpus)
        self.ingest = frozenset(ingest_cpus)
        self.others = frozenset(others)

    @property
    def hot(self):
        return self.fft | self.ingest

    @property
    def layout(self):
        if not self.fft or not self.ingest:
            return None
        if self.topology.is_sibling_pair(self.hot):
            return PAIR
        if len(self.fft) == 1 and len(self.ingest) == 1:
            fft_core = self.topology.siblings.get(min(self.fft), self.fft)
            if min(self.ingest) not in fft_core:
                return SPLIT
        return None

    def idle_siblings(self):
        """Under SPLIT, the other logical CPUs of the two hot cores."""
        cores = set()
        for cpu in self.hot:
            cores |= self.topology.siblings.get(cpu, frozenset([cpu]))
        return frozenset(cores) - self.hot


def choose_split(topology, isolated, avoid=frozenset()):
    """Two physical cores for the split layout: (fft_cpu, ingest_cpu, isolate,
    reason). `isolate` is both cores whole, so nothing runs beside either hot
    thread. Same preference order as choose_pair; never the boot CPU's core."""
    boot = topology.boot_cpu
    candidates = []
    for pair in topology.sibling_pairs():
        if boot in pair:
            continue
        fully_isolated = frozenset(pair) <= frozenset(isolated)
        clean = not (frozenset(pair) & frozenset(avoid))
        candidates.append(((not fully_isolated, not clean, min(pair)), pair))
    if len(candidates) < 2:
        return None, None, frozenset(), (
            "the split layout needs two physical cores besides the boot CPU's "
            "(%d); this machine has %d" % (boot, len(candidates)))
    candidates.sort()
    (a_key, a), (b_key, b) = candidates[0], candidates[1]
    reasons = []
    if a_key[0] or b_key[0]:
        reasons.append("not fully isolated")
    if a_key[1] or b_key[1]:
        reasons.append("carries a high-rate interrupt")
    reason = ("best available, but " + " and ".join(reasons) if reasons
              else "two fully isolated cores, no high-rate interrupt")
    return min(a), min(b), frozenset(a) | frozenset(b), reason
