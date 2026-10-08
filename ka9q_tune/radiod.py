"""R4: find radiod, read where its threads actually run, and place them well.

radiod's FFT and ingest threads share L1 and L2 when they sit on the two
logical CPUs of one physical core, so the target is a hyperthread sibling pair
-- read from topology, never inferred from CPU numbering -- that is fully
isolated and is not the boot CPU.
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
                os.sched_setaffinity(tid, cpus)
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
