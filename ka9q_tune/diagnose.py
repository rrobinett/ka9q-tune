"""The discriminator: when both hot threads scale together, blame the core.

proc_rx888 is the USB sample-ingest thread and performs no FFT. On the station
this package exists because of, it improved by the same factor as the FFT
thread when isolation was applied -- 1.81x and 1.82x -- and no explanation
involving FFT plans can account for that.

So the rule is: when both of radiod's hot threads move together, the cause is
the core (tick, scheduling, interrupt service), never the transform. That
signature was present in the very first measurement taken on the bad station
and was walked past three times while cache and FFTW wisdom were investigated
instead.

What is NOT the discriminator: the ratio between the two threads. Across every
station measured, good and bad, fft/proc_rx888 sat between 2.19 and 2.42 --
including the 94.2/41.6 reading from the broken one. The ratio is an invariant
of the workload, not a health signal. What tells you something is whether both
figures are elevated against a known-good reading of the same station.
"""

import json
import time

from . import procfs, radiod as radiod_mod

# Measured 2026-10-02/03 at 129.6 Msps with complete FFTW wisdom, on 6- and
# 8-core mobile Ryzen parts, mostly in KVM guests. Per-thread CPU time from
# /proc/<pid>/task/<tid>/stat over 30-60 s on a settled station.
FLEET = [
    # station,     fft,   proc_rx888, note
    ("AC0G-B4",    47.4,  21.5, "Ryzen 7 5825U, 10 MiB of 16, isolated 12-13"),
    ("W3USR-06",   50.4,  22.1, "Ryzen 7 5825U, 10 MiB of 16, isolated 12,13"),
    ("AI6VN",      59.0,  24.4, "Ryzen 5 5560U, 5 MiB of 8, isolated 8-9"),
    ("WB6CXC-7",   48.1,  22.0, "Ryzen 5 5560U, 5 MiB of 8, isolated 8-9"),
]
FLEET_BAD = ("WB6CXC-7 before", 94.2, 41.6, "same part, no isolation at all")

FLEET_FFT = sum(row[1] for row in FLEET) / len(FLEET)
FLEET_INGEST = sum(row[2] for row in FLEET) / len(FLEET)

ELEVATED = 1.25          # factor above reference that counts as elevated
TOGETHER = 1.25          # how close the two elevations must be to be "together"
SATURATED = 85.0         # a thread this close to a whole core is in trouble

CORE = "CORE"
PLANS = "PLANS"
INGEST = "INGEST"
MIXED = "MIXED"
OK = "OK"
UNKNOWN = "UNKNOWN"


class Reading:
    def __init__(self, fft, ingest, seconds, threads=None):
        self.fft = fft
        self.ingest = ingest
        self.seconds = seconds
        self.threads = threads or {}

    @property
    def ratio(self):
        if not self.ingest:
            return None
        return self.fft / self.ingest

    def as_dict(self):
        return {"fft": self.fft, "proc_rx888": self.ingest, "seconds": self.seconds}


class Verdict:
    def __init__(self, code, headline, detail, reference_source):
        self.code = code
        self.headline = headline
        self.detail = detail
        self.reference_source = reference_source

    def __repr__(self):
        return "Verdict(%s)" % self.code


def sample(env, radiod, seconds=30.0, sleep=None):
    """Measure the two hot threads over `seconds`. Returns a Reading."""
    sleep = sleep or env.sleep
    if not radiod.running:
        return Reading(None, None, seconds)
    before = procfs.ThreadSample.read(env, radiod.pid)
    sleep(seconds)
    after = procfs.ThreadSample.read(env, radiod.pid)
    return reading_from(before, after, seconds, env.clock_ticks)


def reading_from(before, after, seconds, clock_ticks):
    """Build a Reading from two samples. Pure, so the live path is testable."""
    percentages = procfs.thread_percentages(before, after, seconds, clock_ticks)
    named = {}
    for tid, pct in percentages.items():
        name = after.names.get(tid) or before.names.get(tid) or str(tid)
        named[name] = named.get(name, 0.0) + pct
    return Reading(named.get(radiod_mod.FFT_THREAD),
                   named.get(radiod_mod.INGEST_THREAD),
                   seconds, named)


# -- baselines -----------------------------------------------------------

def save_baseline(env, reading, context=None):
    path = env.path("BASELINE")
    import os

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    payload = dict(reading.as_dict())
    payload["saved_at"] = env.now()
    payload["context"] = context or {}
    return env.write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_baseline(env):
    raw = env.read(env.path("BASELINE"))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if data.get("fft") is None or data.get("proc_rx888") is None:
        return None
    return data


def reference(env):
    """(fft, ingest, source). A station's own baseline beats the fleet mean."""
    saved = load_baseline(env)
    if saved:
        return saved["fft"], saved["proc_rx888"], "this station's saved baseline"
    return FLEET_FFT, FLEET_INGEST, (
        "the fleet mean (%.1f / %.1f) -- other hardware, so treat elevation "
        "against it as a hint, not a measurement" % (FLEET_FFT, FLEET_INGEST)
    )


# -- the verdict ---------------------------------------------------------

def verdict(reading, ref_fft, ref_ingest, source):
    if reading.fft is None or reading.ingest is None:
        missing = []
        if reading.fft is None:
            missing.append(radiod_mod.FFT_THREAD)
        if reading.ingest is None:
            missing.append(radiod_mod.INGEST_THREAD)
        return Verdict(UNKNOWN,
                       "cannot discriminate: thread(s) not found: %s"
                       % ", ".join(missing),
                       ["Without both threads there is no signature to read."],
                       source)

    elev_fft = reading.fft / ref_fft if ref_fft else None
    elev_ing = reading.ingest / ref_ingest if ref_ingest else None
    detail = [
        "fft %.1f%% vs reference %.1f%% (%.2fx)" % (reading.fft, ref_fft, elev_fft),
        "proc_rx888 %.1f%% vs reference %.1f%% (%.2fx)"
        % (reading.ingest, ref_ingest, elev_ing),
    ]
    if reading.ratio:
        detail.append(
            "fft/proc_rx888 = %.2f (an invariant of the workload across every "
            "station measured, good and bad -- not a health signal)"
            % reading.ratio
        )

    fft_hot = elev_fft >= ELEVATED
    ing_hot = elev_ing >= ELEVATED

    if not fft_hot and not ing_hot:
        if reading.fft >= SATURATED:
            return Verdict(
                MIXED,
                "fft is at %.1f%% of a core -- near saturation despite matching "
                "the reference" % reading.fft,
                detail + ["The reference itself may have been taken on an "
                          "already-impaired station."],
                source,
            )
        return Verdict(OK, "both hot threads are at reference", detail, source)

    if fft_hot and ing_hot:
        together = (elev_fft / elev_ing) if elev_ing else 0
        if 1.0 / TOGETHER <= together <= TOGETHER:
            return Verdict(
                CORE,
                "both hot threads scale together (%.2fx and %.2fx): the cause "
                "is the CORE -- tick, scheduling or interrupt service -- not "
                "the transform" % (elev_fft, elev_ing),
                detail + [
                    "proc_rx888 performs no FFT. No explanation involving FFT "
                    "plans or cache can account for it moving with the FFT "
                    "thread.",
                    "Check, in this order: delivered nohz_full for radiod's "
                    "cores, the local timer interrupt rate on those cores, and "
                    "any high-rate IRQ landing on them.",
                ],
                source,
            )
        return Verdict(
            MIXED,
            "both threads are elevated but by different factors (%.2fx and "
            "%.2fx)" % (elev_fft, elev_ing),
            detail + ["More than one cause, or a reference taken under a "
                      "different channel load."],
            source,
        )

    if fft_hot:
        return Verdict(
            PLANS,
            "fft alone is elevated (%.2fx) while proc_rx888 is at reference: "
            "the cause is on the TRANSFORM side" % elev_fft,
            detail + [
                "Check fft.log for ESTIMATE plans, then the L3 allocation.",
                "A core-level cause would have moved proc_rx888 too.",
            ],
            source,
        )

    return Verdict(
        INGEST,
        "proc_rx888 alone is elevated (%.2fx) while fft is at reference: the "
        "cause is on the INGEST side" % elev_ing,
        detail + ["Look at the USB host controller, its IRQ placement and its "
                  "rate, before looking at radiod at all."],
        source,
    )
