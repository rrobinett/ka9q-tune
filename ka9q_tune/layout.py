"""Measure which placement of radiod's hot threads is faster on THIS machine.

The pair layout (fft and proc_rx888 on one physical core's two hyperthreads)
was measured faster on mobile Ryzen; the split layout (each on its own core)
was measured faster on a Skylake-SP Xeon. A rule cannot know which machine it
is on, so this measures, on the running radiod:

    A  pair    fft on cpu a, proc_rx888 on a's sibling
    B  split   fft on cpu a, proc_rx888 on a cpu of another core
    A  pair    again, so drift during the run is visible

Only the ingest thread's core changes between windows; fft stays on cpu a.
Every hot thread's affinity is restored afterwards, whatever happens.
"""

from . import diagnose
from . import radiod as radiod_mod

# A difference smaller than this many points of one core, or than twice the
# drift between the two pair windows, is not a measurement.
MIN_DIFFERENCE = 2.0


class Window:
    def __init__(self, layout, fft_cpu, ingest_cpu, reading):
        self.layout = layout
        self.fft_cpu = fft_cpu
        self.ingest_cpu = ingest_cpu
        self.fft = reading.fft
        self.ingest = reading.ingest


class Result:
    def __init__(self, windows, verdict, reason):
        self.windows = windows
        self.verdict = verdict      # radiod_mod.PAIR, radiod_mod.SPLIT or None
        self.reason = reason


def candidates(topology, isolated, current=frozenset()):
    """(a, a_sibling, b): fft cpu, its sibling, and an ingest cpu on another core.

    Prefers the cpus radiod's hot threads already use, then isolated cores,
    never the boot CPU's core. None when the machine cannot host both layouts.
    """
    boot_core = topology.siblings.get(topology.boot_cpu, frozenset([topology.boot_cpu]))
    pairs = [p for p in topology.sibling_pairs() if len(p) == 2 and not (p & boot_core)]
    if len(pairs) < 2:
        return None

    def rank(pair):
        return (not (pair & frozenset(current)),
                not (pair <= frozenset(isolated)), min(pair))

    pairs.sort(key=rank)
    first, second = pairs[0], pairs[1]
    a = min(first & frozenset(current)) if first & frozenset(current) else min(first)
    return a, min(first - {a}), min(second)


def verdict(windows):
    """Compare the split window with the mean of the two pair windows."""
    pair1, split, pair2 = windows
    if None in (pair1.fft, split.fft, pair2.fft):
        return None, "a window found no fft thread to measure"
    pair_fft = (pair1.fft + pair2.fft) / 2
    drift = abs(pair1.fft - pair2.fft)
    margin = max(MIN_DIFFERENCE, 2 * drift)
    gain = pair_fft - split.fft
    if gain > margin:
        return radiod_mod.SPLIT, (
            "fft is %.1f points lower split than as a pair (%.1f%% vs %.1f%%), "
            "more than the %.1f-point margin" % (gain, split.fft, pair_fft, margin))
    if -gain > margin:
        return radiod_mod.PAIR, (
            "fft is %.1f points lower as a pair than split (%.1f%% vs %.1f%%), "
            "more than the %.1f-point margin" % (-gain, pair_fft, split.fft, margin))
    return None, (
        "no difference beyond the %.1f-point margin (pair %.1f%%, split %.1f%%, "
        "drift %.1f between the pair windows); either layout will do"
        % (margin, pair_fft, split.fft, drift))


def measure(env, radiod, a, a_sibling, b, seconds=30.0, settle=2.0, sleep=None):
    """Run A/B/A on the live radiod and restore its placement. Returns Result."""
    sleep = sleep or env.sleep
    fft_tids, ingest_tids = radiod.hot_tids()
    saved = {tid: radiod.affinity(tid) for tid in fft_tids + ingest_tids}
    plan = [(radiod_mod.PAIR, a, a_sibling), (radiod_mod.SPLIT, a, b),
            (radiod_mod.PAIR, a, a_sibling)]
    windows = []
    try:
        for layout, fft_cpu, ingest_cpu in plan:
            for tid in fft_tids:
                radiod.set_affinity(tid, {fft_cpu})
            for tid in ingest_tids:
                radiod.set_affinity(tid, {ingest_cpu})
            sleep(settle)
            reading = diagnose.sample(env, radiod, seconds=seconds, sleep=sleep)
            windows.append(Window(layout, fft_cpu, ingest_cpu, reading))
    finally:
        for tid, cpus in saved.items():
            if cpus:
                try:
                    radiod.set_affinity(tid, cpus)
                except OSError:
                    pass
    best, reason = verdict(windows)
    return Result(windows, best, reason)


def render(result):
    lines = ["%-6s fft on cpu%-3d proc_rx888 on cpu%-3d   fft %5s   proc_rx888 %5s"
             % (w.layout, w.fft_cpu, w.ingest_cpu, _pct(w.fft), _pct(w.ingest))
             for w in result.windows]
    lines.append("")
    if result.verdict:
        lines.append("faster here: %s -- %s" % (result.verdict, result.reason))
        lines.append("apply it with `ka9q-tune stage --layout %s` and "
                     "`ka9q-tune apply --layout %s`" % (result.verdict, result.verdict))
    else:
        lines.append("no measurable winner: %s" % result.reason)
    return lines


def _pct(value):
    return "-" if value is None else "%.1f%%" % value
