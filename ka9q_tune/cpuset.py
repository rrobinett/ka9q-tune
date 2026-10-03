"""CPU lists, as sets.

A grub.d drop-in renders `12-13`; a hand-written kernel command line may carry
`12,13`. They mean the same thing. A tool that compares the two as strings
decides the machine is misconfigured, reboots it, finds the same difference,
and reboots it again, forever, chasing a difference that is not one.

So: parse to a frozenset, compare sets, and format canonically only for display.
"""


def parse(text):
    """Parse a kernel-style CPU list into a frozenset of ints.

    Accepts `8-9`, `12,13`, `0-3,8,12-13`, whitespace, an empty string, and the
    `(null)` that sysfs prints when a list is unset. Unparseable fragments are
    skipped rather than raising: this runs on live machines and a surprise in
    one field must not take the whole report down.
    """
    out = set()
    if not text:
        return frozenset()
    text = text.strip()
    if text in ("(null)", "none", "-"):
        return frozenset()
    for chunk in text.replace(" ", ",").split(","):
        if not chunk:
            continue
        if "-" in chunk[1:]:
            lo, _, hi = chunk.partition("-")
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            if hi_i >= lo_i and hi_i - lo_i < 65536:
                out.update(range(lo_i, hi_i + 1))
        else:
            try:
                out.add(int(chunk))
            except ValueError:
                continue
    return frozenset(out)


def format(cpus):
    """Format a set of CPUs canonically, collapsing runs: {8,9,12} -> '8-9,12'."""
    cpus = sorted(cpus)
    if not cpus:
        return ""
    parts = []
    start = prev = cpus[0]
    for cpu in cpus[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        parts.append(_run(start, prev))
        start = prev = cpu
    parts.append(_run(start, prev))
    return ",".join(parts)


def _run(start, end):
    if start == end:
        return str(start)
    if end == start + 1:
        return "%d,%d" % (start, end)
    return "%d-%d" % (start, end)


def to_mask(cpus):
    """Format a CPU set as the hex bitmask smp_affinity wants."""
    value = 0
    for cpu in cpus:
        value |= 1 << cpu
    if value == 0:
        return "0"
    digits = "%x" % value
    # 32-bit groups, comma separated, the way /proc/irq/*/smp_affinity reads.
    digits = digits.rjust(((len(digits) + 7) // 8) * 8, "0")
    return ",".join(digits[i:i + 8] for i in range(0, len(digits), 8))


def equal(a, b):
    """True when two CPU lists denote the same set, whatever their spelling."""
    return parse(a) == parse(b) if isinstance(a, str) or isinstance(b, str) else frozenset(a) == frozenset(b)
