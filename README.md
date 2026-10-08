# ka9q-tune

A stand-alone Linux service that applies, verifies and keeps the CPU, cache and
interrupt conditions `radiod` needs to run an RX888 reliably at high sample
rates — and says so loudly when it cannot.

This exists because a station was unusable for sixteen hours while every health
signal read green. Fixing it took one kernel command line. Finding it took a
day, because nothing in radiod, systemd or the station tooling reports this
class of impairment — not as a dropped frame, not as a failed unit, not as a
log line.

Status: implementation of the AC0G/AI6VN specification. Python 3, standard
library only, no dependencies.

---

## What it does

```
ka9q-tune status            every reading, with the two hot threads side by side
ka9q-tune check             terse; the exit code carries the verdict
ka9q-tune explain           what the three controls do, and the traps

ka9q-tune stage             write the grub.d drop-in (never reboots)
ka9q-tune isolate-oneshot   boot-time unit: apply staged isolation, one reboot
ka9q-tune apply             pin radiod, partition L3, pin frequency, refuse bad IRQs

ka9q-tune pin               thread placement alone
ka9q-tune cache --mib 5     L3 partition alone
ka9q-tune freq              frequency pin alone
ka9q-tune irq --move        retarget high-rate interrupts off the isolated cores
ka9q-tune wisdom --plan     converge on fft.log until it is empty
ka9q-tune baseline          record this station's healthy thread figures
```

`--dry-run` works on all of them and writes nothing.

## What a healthy station looks like

```
radiod            radiod@WB6CXC-7  pid 193916   cpus 8,9 (core 4, sibling pair)
isolation         active   isolcpus=8,9 nohz_full=8,9 rcu_nocbs=8,9
  delivered       isolcpus=8,9 nohz_full=8,9 rcu_nocbs=8,9   (sysfs and rcuo kthreads, not /proc/cmdline)
  tick            0.4/s on cpu8   0.4/s on cpu9
  staged          isolcpus=8,9 nohz_full=8,9 rcu_nocbs=8,9   (applied)
frequency         3200 MHz delivered, min == max
L3 partition      radiod 5.00 MiB of 8.00 MiB (10/16 ways)   past the knee
device IRQs       none above 100/s on 8,9   (housekeeping: 0-7,10,11)
fftw wisdom       fft.log empty   no ESTIMATE plans

threads           fft 48.1%          proc_rx888 22.0%
                    both scaling together would indicate the CORE, not the plans

diagnosis         both hot threads are at reference
```

## And the station that cost the day

```
isolation         NOT ACTIVE   isolcpus=- nohz_full=- rcu_nocbs=-            [BAD]
  tick            256.9/s on cpu8   256.9/s on cpu9                         [BAD]
  staged          isolcpus=8,9 nohz_full=8,9 rcu_nocbs=8,9   STAGED, NOT APPLIED   [BAD]
                    The drop-in was modified AFTER the running kernel booted,
                    so this configuration has never been loaded. Correct on
                    disk, absent from the running system.
frequency         3200 MHz delivered, min == max                            [ok]
L3 partition      radiod 5.00 MiB of 8.00 MiB (10/16 ways)                  [ok]
fftw wisdom       fft.log empty                                             [ok]

threads           fft 94.2%          proc_rx888 41.6%

diagnosis         both hot threads scale together (1.84x and 1.85x): the cause
                  is the CORE -- tick, scheduling or interrupt service -- not
                  the transform
```

Note what is still green in that second report. The L3 partition is correct.
The frequency is pinned. There are no ESTIMATE plans. Every systemd unit is
running. That is exactly how it hid.

## The discriminator

`proc_rx888` is the USB sample-ingest thread and performs **no FFT**. On the
station this was built for, it improved by the same factor as the FFT thread
when isolation was applied — 1.81× and 1.82× — and no explanation involving FFT
plans can account for that.

> When both of radiod's hot threads scale together, the cause is the core —
> tick, scheduling, interrupt service — never the transform.

That signature was present in the first measurement taken and was walked past
three times while cache and FFTW wisdom were investigated instead. A diagnostic
that reports the two threads side by side turns a day into four minutes.

What is *not* the discriminator, and this matters: the **ratio** between the two
threads. Across every station measured, good and bad, `fft/proc_rx888` sat
between 2.19 and 2.42 — including the 94.2/41.6 reading from the broken one.
The ratio is an invariant of the workload. What tells you something is whether
both figures are elevated against a known-good reading, which is what
`ka9q-tune baseline` is for. Without a baseline the fleet mean is used instead,
and the report says so every time.

## Requirements implemented

| | |
|---|---|
| **R1** | Delivered state, never configured state. sysfs for `nohz_full`/`isolcpus`, the `rcuo` kthreads for `rcu_nocbs`, and the `LOC` counter in `/proc/interrupts` as ground truth. |
| **R2** | Staged vs applied: the drop-in is parsed, the running command line is parsed, the two are compared **as sets**, and the drop-in's mtime is compared against `/proc/stat` btime. Checked on every boot, not once at install. |
| **R3** | One reboot per staged configuration, marker written **before** the reboot. Exit 0 / 10 / 20. An unsupported kernel, a failed `update-grub` and a stale `grub.cfg` are each a distinct named stuck state, not a retry. |
| **R4** | Sibling pairs read from `thread_siblings_list`, never inferred from the numbering. The boot CPU is refused. |
| **R5** | L3 expressed in bytes; the mask is computed from live geometry and verified against the kernel's own `<group>/size`. The default group is reduced to the complement, so the allocation is a partition rather than an overlap. |
| **R6** | Per-CPU interrupt *counts* as the evidence, not `smp_affinity`. A `nohz_full` core with a co-located high-rate IRQ is refused outright; `--move-irqs` retargets instead. |
| **R7** | `min == max`, written in an order no intermediate state rejects, then verified from `cpuinfo_cur_freq` — and reported as unverified where only `scaling_cur_freq` exists. |
| **R8** | `fft.log` is reported, and `wisdom --plan` converges on it: plan what it names, clear it, restart, look again, until a full restart leaves it empty. It does not enumerate, because a static list of transform sizes can never be complete. Planning uses radiod's own `fft-gen` when installed, which reads every placement radiod logs (`i`, `o`, and `d` for input-destroying) and writes the version-named wisdom file radiod loads; `fftwf-wisdom` is the fallback for older installs. The restart targets the unit radiod actually runs under (`radiod@` or `ka9q-radio@`). A log last written before radiod started is reported as stale, not as misses. |

## Constraints it keeps

- **Never report success from configuration.** Every claim is backed by a
  reading taken after the fact. `tools/mutate.py` includes mutations that
  remove each read-back, and the suite catches all of them.
- **Never fail silently.** Where a reading could not be taken, the report says
  so in the line where the reading belongs. `absent` and `empty` are different
  words on purpose.
- **Never reboot a station that is capturing** without an operator decision.
  `stage` writes the drop-in and tells you it has not been applied. `apply`
  never reboots at all. Only the boot-time one-shot reboots, and only once.
- **Never assume a VM.** Nothing here reads a hypervisor flag.
- **Never combine `nohz_full` with a co-located high-rate IRQ.** Detected and
  refused.

## Everything is injectable

This is code that reboots machines and rewrites kernel parameters, so it is
fully testable without a machine. `KA9Q_TUNE_ROOT` prefixes `/proc`, `/sys` and
`/etc`; point it at a fixture directory and the whole program reads a fake
machine. Individual paths and commands override that:

```
KA9Q_TUNE_ROOT          fixture root for all the default paths
KA9Q_TUNE_ISOL_CFG      grub.d drop-in to read the staged set from
KA9Q_TUNE_ISOL_MARKER   one-shot marker; its presence forbids a second reboot
KA9Q_TUNE_PROC_CMDLINE  file to read the running kernel's cmdline from
KA9Q_TUNE_REBOOT        command to run instead of `systemctl reboot`
KA9Q_TUNE_GRUB_UPDATE   command to run instead of `update-grub`
KA9Q_TUNE_FFT_LOG       radiod's FFT planning log
KA9Q_TUNE_FFT_GEN       radiod's planner (default `fft-gen`, looked up on PATH)
KA9Q_TUNE_PLANNER       force `fft-gen` or `fftwf-wisdom`
KA9Q_TUNE_RADIOD_RESTART  restart command; default restarts radiod's own unit
KA9Q_TUNE_WISDOM_TIMEOUT  seconds before planning is abandoned (default: none)
KA9Q_TUNE_L3_BYTES      L3 target in bytes (default 5 MiB, the measured knee)
KA9Q_TUNE_IRQ_HIGH_RATE interrupts/s on one CPU before it counts as high
KA9Q_TUNE_DRY_RUN       make no changes
```

The four `SIGMOND_ISOL_*` names from the precedent tool are accepted as aliases
for the ones they correspond to, so existing unit files keep working.

## Tests

```
./run-tests.sh
```

132 unit tests over fixture `/proc` and `/sys` trees, then a mutation run.

**The mutation run is the point.** Write the test, then break the fix and
confirm the test fails — a test nobody has watched fail is not a test.
`tools/mutate.py` contains 23 mutations, each a plausible way to write this
package wrong and most of them the way it was actually written wrong somewhere.
Each one is applied to a copy of the source, the suite is run against it, and a
mutation that *survives* is reported as a hole:

```
caught R1: read the kernel command line instead of sysfs
caught R2: ignore the drop-in's mtime against boot time
caught R2: compare CPU lists as strings, not sets
caught R3: write the marker AFTER the reboot instead of before
...
all 23 mutations caught
```

Two traps from building the precedent are encoded in
`tests/test_source_guards.py`:

- A guard asserted `grep -q _backoff`, which still matched the **function
  definition** after the call site had been deleted. So the guards here assert
  on call sites via the AST, in source order — the marker-before-reboot
  ordering is checked that way and nothing else can satisfy it.
- Assertions matched the tool's own comments, because these files quote the
  buggy line verbatim while explaining the fix. So comments and docstrings are
  stripped before any source assertion, and there is a test *of the stripper*
  that fails if it ever stops stripping.

## Install

```
sudo ./install.sh
ka9q-tune status
```

Nothing is enabled and nothing is changed until you say so. `install.sh` prints
the sequence.

## Limits of the evidence

All the figures this package carries as reference were measured at **129.6 Msps
wideband** on 6- and 8-core mobile Ryzen parts, mostly in KVM guests, with
per-thread CPU time from `/proc/<pid>/task/<tid>/stat` sampled over 30–60 s on
settled stations.

- At 64.8 Msps the front-end transform is half the size and headroom is far
  greater; these effects should be much less sharp.
- For narrowband RX888 use they may not apply at all.
- The gaps-per-channel-hour figures behind R6 (0.68 against 20.82) come from a
  separate experiment on different hardware than the CPU percentages. They are
  directionally reliable, not a matched comparison.
- The specific numbers are not claimed to transfer to other hardware. The
  mechanisms and the traps should. Run `ka9q-tune baseline` on a station you
  believe is healthy and the reference becomes that station's own.

## On virtualised hosts

Where radiod runs in a guest, isolation is needed on **both** sides: the host so
the vCPU thread is not migrated or preempted, the guest so its own kernel does
not schedule on radiod's vCPU.

The station that lost sixteen hours had a correctly configured host —
`isolcpus=0-9`, `nohz_full=1-9` — and a guest with nothing at all. Every
host-side check passed.

This tool reports whichever side it is running on, and cannot see the other.
Run it in both. Two things make the layering legible: a guest tick is injected
through KVM and costs host-side work too (~170 interrupts/s per core appeared on
the *host* purely because the guest was ticking, and vanished when the guest
went tickless), and the two layers can have different `CONFIG_HZ` — 1000 and 250
on the measured pair — so one tells you little about the other.

## License

GPL-3.0-or-later — the same terms as ka9q-radio, so the companion tool carries
the same license as the thing it tunes. Full text in [LICENSE](LICENSE).

ka9q-tune is a separate program. It reads `/proc` and `/sys`, and reads
radiod's own `fft.log`; it links against nothing from ka9q-radio.

---

Specification: AC0G / AI6VN, HamSCI. Measurements 2026-10-02 and 2026-10-03 on
AC0G-B4, W3USR-06, AI6VN and WB6CXC-7.
