"""R6: keep high-rate interrupts off the isolated cores -- and refuse otherwise.

Every interrupt is a kernel entry, and on a nohz_full core each entry and exit
costs context-tracking work. A ~1 kHz interrupt pinned to the same core means
the CPU never stays tickless: you pay all of nohz_full's cost and get none of
its benefit.

Measured on this fleet, as gaps per channel-hour:

    no isolation, interrupt elsewhere      0.68
    isolation, interrupt co-located       20.82

So the combination is treated as a configuration error and refused, rather
than applied with a warning.

(That pair of figures comes from a separate experiment on different hardware
than the CPU percentages elsewhere in this package. Directionally reliable,
not a matched comparison.)
"""

import os

from . import cpuset

DEFAULT_HIGH_RATE = 100.0    # interrupts/s on one CPU before it counts as high

# Per-CPU counters in /proc/interrupts that are not device interrupts and have
# no /proc/irq/<n> to retarget. LOC in particular is the tick itself, measured
# separately as the ground truth for R1; it is not something to move.
NON_DEVICE = {
    "NMI", "LOC", "SPU", "PMI", "IWI", "RTR", "RES", "CAL", "TLB", "TRM",
    "THR", "DFR", "MCE", "MCP", "ERR", "MIS", "PIN", "NPI", "PIW", "HYP",
    "HRE", "HVS", "POS", "IPI", "GIC", "DBI",
}


class Finding:
    def __init__(self, key, label, cpu, rate, affinity, effective):
        self.key = key
        self.label = label
        self.cpu = cpu
        self.rate = rate
        self.affinity = affinity        # configured smp_affinity_list
        self.effective = effective      # effective_affinity_list, what the kernel uses

    @property
    def movable(self):
        return self.key.isdigit()

    def __repr__(self):
        return "Finding(irq=%s cpu=%d rate=%.1f/s)" % (self.key, self.cpu, self.rate)


def affinity_of(env, key):
    """Configured and effective affinity of a numeric IRQ, as CPU sets."""
    if not key.isdigit():
        return None, None
    base = os.path.join(env.path("PROC"), "irq", key)
    configured = cpuset.parse(env.read_stripped(os.path.join(base, "smp_affinity_list"), ""))
    effective = cpuset.parse(env.read_stripped(os.path.join(base, "effective_affinity_list"), ""))
    return configured, (effective or configured)


def irq_name(env, key):
    """A readable name for an IRQ: its handler directory, else its label."""
    if not key.isdigit():
        return key
    base = os.path.join(env.path("PROC"), "irq", key)
    for entry in env.listdir(base):
        full = os.path.join(base, entry)
        if os.path.isdir(full) and entry not in ("smp_affinity", "affinity_hint"):
            return entry
    return "irq%s" % key


def conflicts(env, rates, labels, cpus, threshold=DEFAULT_HIGH_RATE):
    """Interrupts landing on `cpus` faster than `threshold`.

    Delivered counts are the evidence, not smp_affinity: an interrupt can be
    configured for one CPU and serviced on another, and only the counts say
    where the work actually happened.
    """
    cpus = frozenset(cpus)
    out = []
    for key, per_cpu in rates.items():
        if key in NON_DEVICE:
            continue
        for cpu, rate in per_cpu.items():
            if cpu in cpus and rate >= threshold:
                configured, effective = affinity_of(env, key)
                out.append(Finding(key, labels.get(key, ""), cpu, rate,
                                   configured, effective))
    out.sort(key=lambda f: -f.rate)
    return out


def conflicting_cpus(findings):
    return frozenset(f.cpu for f in findings)


def retarget(env, finding, housekeeping):
    """Move one IRQ onto housekeeping CPUs. Returns (ok, message)."""
    if not finding.movable:
        return False, ("%s is a per-CPU kernel counter, not a retargetable IRQ"
                       % finding.key)
    if not housekeeping:
        return False, "no housekeeping CPUs to move IRQ %s to" % finding.key
    path = os.path.join(env.path("PROC"), "irq", finding.key, "smp_affinity_list")
    ok, detail = env.write(path, cpuset.format(housekeeping) + "\n")
    if not ok:
        return False, detail
    # Verify, rather than report success from the write.
    now = cpuset.parse(env.read_stripped(path, ""))
    if not env.dry_run and now and not now <= frozenset(housekeeping):
        return False, ("IRQ %s still reads %s after the write"
                       % (finding.key, cpuset.format(now)))
    return True, "IRQ %s (%s) moved to %s" % (
        finding.key, irq_name(env, finding.key), cpuset.format(housekeeping))


def housekeeping_cpus(topology, isolated):
    """CPUs that are not isolated, which is where interrupts belong."""
    rest = frozenset(topology.online) - frozenset(isolated)
    return rest or frozenset([topology.boot_cpu])
