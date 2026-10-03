"""Command line. One command gives an operator the whole picture.

Subcommands:

  status            every reading, with the two hot threads side by side
  check             the same, terse, exit code carries the verdict
  isolate-oneshot   the boot-time unit (R2/R3); exits 0 / 10 / 20
  stage             write the grub.d drop-in for a chosen sibling pair
  apply             pin radiod, partition L3, pin frequency, refuse bad IRQs
  pin / cache / freq / irq / wisdom / baseline   the pieces, individually
  explain           what the three isolation controls actually do
"""

import argparse
import os
import sys

from . import cache, cpuset, diagnose, fftw, freq as freq_mod, irq as irq_mod
from . import isolation, procfs, radiod as radiod_mod, report, topology as topo_mod
from .env import Env

EXIT_OK = 0
EXIT_WARN = 1
EXIT_BAD = 2
EXIT_REFUSED = 3
EXIT_REBOOT_REQUIRED = 4


def _machine(env):
    topology = topo_mod.Topology(env)
    return topology, radiod_mod.Radiod(env, topology), isolation.State(env)


def _sample_interrupts(env, seconds, sleep=None):
    sleep = sleep or env.sleep
    before = procfs.Interrupts.parse(env.read(env.path("PROC_INTERRUPTS"), ""))
    sleep(seconds)
    after = procfs.Interrupts.parse(env.read(env.path("PROC_INTERRUPTS"), ""))
    return (procfs.interrupt_rates(before, after, seconds),
            procfs.tick_rates(before, after, seconds),
            after.labels)


def _emit(lines, out):
    for line in lines:
        print(line, file=out)


# -- subcommands ----------------------------------------------------------

def cmd_status(env, args, out):
    status = report.collect(env, seconds=args.seconds)
    print(report.render(status, verbose=not args.terse), file=out)
    return status.exit_code


def cmd_check(env, args, out):
    status = report.collect(env, seconds=args.seconds)
    if status.problems:
        for state, label, value in status.problems:
            print("%-5s %-18s %s" % (state.upper(), label.strip(), value), file=out)
    else:
        print("ok   every checked condition is delivered", file=out)
    return status.exit_code


def cmd_oneshot(env, args, out):
    result = isolation.one_shot(env)
    for message in result.messages:
        print(message, file=out)
    print("status: %s" % result.status, file=out)
    return result.code


def cmd_stage(env, args, out):
    topology, radiod, state = _machine(env)
    cpus = _target_cpus(env, args, topology, radiod, state, out)
    if not cpus:
        return EXIT_REFUSED
    body = (
        "# Written by ka9q-tune. The three parameters only work as a set:\n"
        "#   isolcpus   keeps a second runnable task off the CPU\n"
        "#   nohz_full  stops the tick, but only with one runnable task\n"
        "#   rcu_nocbs  offloads callbacks that would force the tick back on\n"
        "GRUB_CMDLINE_LINUX_DEFAULT=\"$GRUB_CMDLINE_LINUX_DEFAULT "
        "isolcpus=%s nohz_full=%s rcu_nocbs=%s\"\n"
        % (cpuset.format(cpus), cpuset.format(cpus), cpuset.format(cpus))
    )
    ok, detail = env.write(env.path("ISOL_CFG"), body)
    print(detail, file=out)
    if not ok:
        return EXIT_BAD
    code, output = isolation.run_command(env, env.command("GRUB_UPDATE"))
    print("%s exited %d%s" % (env.command("GRUB_UPDATE"), code,
                              (": " + output) if output else ""), file=out)
    if code != 0:
        return EXIT_BAD
    print("staged isolcpus/nohz_full/rcu_nocbs = %s" % cpuset.format(cpus), file=out)
    print("NOT APPLIED until the kernel is reloaded. This command does not "
          "reboot: a station that is capturing must not be rebooted without an "
          "operator decision. Reboot when convenient, or let the boot-time "
          "one-shot do it at the next restart.", file=out)
    return EXIT_REBOOT_REQUIRED


def cmd_apply(env, args, out):
    topology, radiod, state = _machine(env)
    rates, ticks, labels = _sample_interrupts(env, args.irq_seconds)
    isolated = state.effective_isolated()

    cpus = _target_cpus(env, args, topology, radiod, state, out, rates=rates)
    if not cpus:
        return EXIT_REFUSED

    # R6, checked before anything is applied. A nohz_full core carrying a
    # high-rate interrupt is a configuration error, not a warning.
    threshold = env.number("IRQ_HIGH_RATE", irq_mod.DEFAULT_HIGH_RATE)
    findings = irq_mod.conflicts(env, rates, labels, cpus, threshold)
    nohz_here = bool(frozenset(cpus) & state.delivered["nohz_full"])
    if findings and (nohz_here or args.assume_isolated):
        housekeeping = irq_mod.housekeeping_cpus(topology, isolated or cpus)
        for finding in findings:
            print("IRQ %s (%s) at %.0f/s on cpu%d, a nohz_full core"
                  % (finding.key, irq_mod.irq_name(env, finding.key),
                     finding.rate, finding.cpu), file=out)
        if not args.move_irqs:
            print("REFUSED: nohz_full core with a co-located high-rate "
                  "interrupt. You would pay all of nohz_full's cost and get "
                  "none of its benefit -- measured at 20.82 gaps per "
                  "channel-hour against 0.68 with the interrupt elsewhere.",
                  file=out)
            print("Re-run with --move-irqs to retarget them to %s, or choose "
                  "different cores." % cpuset.format(housekeeping), file=out)
            return EXIT_REFUSED
        for finding in findings:
            ok, detail = irq_mod.retarget(env, finding, housekeeping)
            print(("  " if ok else "  FAILED: ") + detail, file=out)
            if not ok:
                return EXIT_BAD

    failures = 0

    ok, messages = radiod.pin(cpus)
    _emit(("  " + m for m in messages), out)
    failures += 0 if ok else 1

    if not args.no_cache:
        resctrl = cache.Resctrl(env, topology)
        target = _cache_target(env, args)
        tids = list(radiod.threads) or ([radiod.pid] if radiod.pid else [])
        ok, messages = resctrl.apply(env.text("L3_GROUP", cache.DEFAULT_GROUP),
                                     target, tids)
        _emit(("  " + m for m in messages), out)
        failures += 0 if ok else 1

    if not args.no_freq:
        khz = args.freq_khz or freq_mod.target_khz(env, cpus)
        if khz:
            for cpu in sorted(cpus):
                ok, messages = freq_mod.CpuFreq(env, cpu).set_pinned(khz)
                _emit(("  " + m for m in messages), out)
                failures += 0 if ok else 1
        else:
            print("  no frequency target available; skipping", file=out)
            failures += 1

    if not frozenset(cpus) <= isolated:
        print("", file=out)
        print("radiod is placed and constrained, but cpus %s are NOT fully "
              "isolated by the running kernel. Stage the parameters with "
              "`ka9q-tune stage` and reload the kernel; until then the tick "
              "keeps firing on these cores."
              % cpuset.format(frozenset(cpus) - isolated), file=out)
        return EXIT_REBOOT_REQUIRED
    return EXIT_OK if not failures else EXIT_BAD


def cmd_pin(env, args, out):
    topology, radiod, state = _machine(env)
    cpus = _target_cpus(env, args, topology, radiod, state, out)
    if not cpus:
        return EXIT_REFUSED
    ok, messages = radiod.pin(cpus)
    _emit(messages, out)
    return EXIT_OK if ok else EXIT_BAD


def cmd_cache(env, args, out):
    topology, radiod, _ = _machine(env)
    resctrl = cache.Resctrl(env, topology)
    group = env.text("L3_GROUP", cache.DEFAULT_GROUP)
    if args.show:
        if not resctrl.available:
            print("resctrl not available at %s" % resctrl.root, file=out)
            return EXIT_WARN
        bits = resctrl.num_bits()
        print("ways in L3: %d" % bits, file=out)
        for domain in resctrl.domains():
            per_way = resctrl.bytes_per_way(domain)
            print("L3:%d total %s, %s per way"
                  % (domain, topo_mod.human_bytes(resctrl.domain_bytes(domain)),
                     topo_mod.human_bytes(int(per_way)) if per_way else "unknown"),
                  file=out)
        for domain, size in sorted(resctrl.group_size(group).items()):
            print("group %s L3:%d = %s" % (group, domain,
                                           topo_mod.human_bytes(size)), file=out)
        return EXIT_OK
    tids = list(radiod.threads) or ([radiod.pid] if radiod.pid else [])
    ok, messages = resctrl.apply(group, _cache_target(env, args), tids)
    _emit(messages, out)
    return EXIT_OK if ok else EXIT_BAD


def cmd_freq(env, args, out):
    topology, radiod, state = _machine(env)
    cpus = radiod.process_affinity() or state.effective_isolated() or frozenset(topology.online)
    if args.show:
        for cpu, f in sorted(freq_mod.survey(env, cpus).items()):
            if not f.present:
                continue
            khz, verified = f.delivered()
            print("cpu%-3d min %-8s max %-8s delivered %-8s %s %s"
                  % (cpu, f.min_khz, f.max_khz, khz,
                     "(hardware)" if verified else "(setpoint echo?)",
                     f.driver or ""), file=out)
        return EXIT_OK
    khz = args.freq_khz or freq_mod.target_khz(env, cpus)
    if not khz:
        print("no frequency target available", file=out)
        return EXIT_BAD
    failures = 0
    for cpu in sorted(cpus):
        ok, messages = freq_mod.CpuFreq(env, cpu).set_pinned(khz)
        _emit(messages, out)
        failures += 0 if ok else 1
    return EXIT_OK if not failures else EXIT_BAD


def cmd_irq(env, args, out):
    topology, radiod, state = _machine(env)
    rates, _, labels = _sample_interrupts(env, args.irq_seconds)
    isolated = state.effective_isolated()
    cpus = radiod.process_affinity() or isolated
    threshold = env.number("IRQ_HIGH_RATE", irq_mod.DEFAULT_HIGH_RATE)
    findings = irq_mod.conflicts(env, rates, labels, cpus, threshold)
    if not findings:
        print("no interrupt above %.0f/s on %s"
              % (threshold, cpuset.format(cpus) or "radiod's cores"), file=out)
        return EXIT_OK
    housekeeping = irq_mod.housekeeping_cpus(topology, isolated or cpus)
    for finding in findings:
        print("IRQ %-6s %-20s %7.1f/s on cpu%-3d configured %s"
              % (finding.key, irq_mod.irq_name(env, finding.key), finding.rate,
                 finding.cpu, cpuset.format(finding.affinity or [])), file=out)
    if not args.move:
        print("Re-run with --move to retarget these to %s."
              % cpuset.format(housekeeping), file=out)
        return EXIT_BAD
    failures = 0
    for finding in findings:
        ok, detail = irq_mod.retarget(env, finding, housekeeping)
        print(detail, file=out)
        failures += 0 if ok else 1
    return EXIT_OK if not failures else EXIT_BAD


def cmd_wisdom(env, args, out):
    misses, unparsed, exists = fftw.read_log(env)
    if not exists:
        print("%s does not exist. Absent is not empty: radiod may not have "
              "run, or may log elsewhere." % env.path("FFT_LOG"), file=out)
        return EXIT_WARN
    if not args.plan:
        if not misses and not unparsed:
            print("fft.log is empty: no transforms on ESTIMATE plans.", file=out)
            return EXIT_OK
        for miss in misses:
            print("%-12s %s" % (miss.spec, miss.line), file=out)
        for line in unparsed:
            print("unparsed: %s" % line, file=out)
        return EXIT_BAD
    ok, messages = fftw.converge(
        env,
        lambda cmd: isolation.run_command(env, cmd),
        settle_seconds=args.settle,
    )
    _emit(messages, out)
    return EXIT_OK if ok else EXIT_BAD


def cmd_baseline(env, args, out):
    topology, radiod, state = _machine(env)
    if not radiod.running:
        print("radiod is not running; nothing to baseline", file=out)
        return EXIT_BAD
    # Refuse before spending the sampling window, not after: a baseline taken
    # on an impaired station becomes the reference every later diagnosis is
    # measured against, and the fault silently becomes "normal".
    status = report.collect(env, seconds=0.0, sleep=lambda _s: None)
    if any(l.state == report.BAD for l in status.lines) and not args.force:
        print("this station has failing checks right now; a baseline taken "
              "here would encode the fault as normal. Re-run with --force if "
              "you mean it.", file=out)
        for s, label, value in status.problems:
            print("  %-5s %-18s %s" % (s.upper(), label.strip(), value), file=out)
        return EXIT_REFUSED
    reading = diagnose.sample(env, radiod, seconds=args.seconds, sleep=env.sleep)
    if reading.fft is None or reading.ingest is None:
        print("could not find both hot threads; not saving a partial baseline",
              file=out)
        return EXIT_BAD
    ok, detail = diagnose.save_baseline(env, reading, {
        "cpus": cpuset.format(radiod.process_affinity()),
        "isolated": cpuset.format(state.effective_isolated()),
    })
    print(detail, file=out)
    print("baseline: fft %.1f%%  proc_rx888 %.1f%%" % (reading.fft, reading.ingest),
          file=out)
    return EXIT_OK if ok else EXIT_BAD


def cmd_explain(env, args, out):
    print(EXPLANATION.strip(), file=out)
    return EXIT_OK


# -- shared helpers -------------------------------------------------------

def _cache_target(env, args):
    if getattr(args, "mib", None):
        return int(args.mib * 1024 * 1024)
    return int(env.number("L3_BYTES", 0)) or cache.DEFAULT_TARGET_BYTES


def _target_cpus(env, args, topology, radiod, state, out, rates=None):
    """The sibling pair to use, from --cpus or chosen from topology."""
    if getattr(args, "cpus", None):
        cpus = cpuset.parse(args.cpus)
        if not cpus:
            print("could not parse --cpus %r" % args.cpus, file=out)
            return frozenset()
        if topology.boot_cpu in cpus:
            print("REFUSED: cpu%d is the boot CPU and can never be nohz_full. "
                  "The kernel drops it from the list silently, so half the "
                  "sibling pair would keep ticking."
                  % topology.boot_cpu, file=out)
            return frozenset()
        if not topology.is_sibling_pair(cpus):
            print("warning: %s is not a hyperthread sibling pair "
                  "(thread_siblings_list says otherwise); the fft and ingest "
                  "threads will not share L1/L2." % cpuset.format(cpus), file=out)
        return cpus
    avoid = frozenset()
    if rates:
        threshold = env.number("IRQ_HIGH_RATE", irq_mod.DEFAULT_HIGH_RATE)
        avoid = frozenset(
            cpu for per_cpu in
            (v for k, v in rates.items() if k not in irq_mod.NON_DEVICE)
            for cpu, rate in per_cpu.items() if rate >= threshold
        )
    isolated = state.effective_isolated() or state.staged.get("nohz_full", frozenset())
    cpus, reason = radiod_mod.choose_pair(topology, isolated, avoid=avoid)
    if not cpus:
        print("REFUSED: %s" % reason, file=out)
        return frozenset()
    print("chose cpus %s (%s): %s"
          % (cpuset.format(cpus), topology.describe(cpus), reason), file=out)
    return cpus


EXPLANATION = """
The three isolation controls are separate mechanisms and they only work as a
set. An implementation that applies one without the others achieves close to
nothing.

  isolcpus=<cpus>
      Removes those CPUs from the scheduler's load-balancing domains. Nothing
      lands there unless explicitly pinned. It does NOT stop timers, interrupts
      or kernel threads -- on its own it is the weakest of the three.

  nohz_full=<cpus>
      Suppresses the periodic scheduler tick. The subtlety that matters: it
      only stops the tick when exactly ONE runnable task is on the CPU. With
      two, the kernel needs the tick to preempt between them and brings it back
      immediately. That is what isolcpus is for. The two are not redundant; one
      enables the other.

  rcu_nocbs=<cpus>
      Offloads RCU callbacks to rcuo kthreads elsewhere. Without it the tick
      cannot stop at all, because pending callbacks force it. A prerequisite,
      not an optimisation.

Measured, one guest, before and after, nothing else changed:

      timer interrupts on radiod's core    256.9/s  ->  0.4/s
      radiod fft                             94.2%  ->  48.1%
      radiod proc_rx888                      41.6%  ->  22.0%

Traps this tool checks for, each of which cost real hours:

  The boot CPU can never be nohz_full. The kernel drops it silently, so a
  command line reading nohz_full=0-9 yields a sysfs file reading 1-9. Always
  read the sysfs file, never trust the command line.

  A drop-in written after the running kernel booted is staged, not applied. One
  station had a correct drop-in, a correctly rebuilt grub.cfg, and a kernel
  booted 41 minutes before the drop-in was written. Every health check passed.

  On a virtualised host, isolation is needed on BOTH sides. One station had a
  correctly configured host -- isolcpus=0-9, nohz_full=1-9 -- and a guest with
  nothing at all. Every host-side check passed. That is exactly how it hid. A
  guest tick is injected through KVM and costs host-side work too, and the two
  layers can have different CONFIG_HZ (1000 and 250 on the measured pair), so
  one tells you little about the other.

  A high-rate interrupt on a nohz_full core costs you all of nohz_full's
  overhead and returns none of its benefit.

Limits of the evidence: all figures are 129.6 Msps wideband on 6- and 8-core
mobile Ryzen parts, mostly in KVM guests. At 64.8 Msps the front-end transform
is half the size and these effects should be much less sharp; for narrowband
RX888 use they may not apply at all. The mechanisms and the traps should
transfer; the specific numbers are not claimed to.
"""


# -- argument parsing -----------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="ka9q-tune",
        description="Verify and maintain the CPU conditions radiod needs.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="make no changes; print what would be written")
    sub = parser.add_subparsers(dest="command")

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        return p

    p = add("status", cmd_status, "every reading, with both hot threads")
    p.add_argument("--seconds", type=float, default=30.0,
                   help="sampling window (default 30)")
    p.add_argument("--terse", action="store_true", help="values only, no notes")

    p = add("check", cmd_check, "terse; exit code carries the verdict")
    p.add_argument("--seconds", type=float, default=10.0)

    add("isolate-oneshot", cmd_oneshot,
        "boot-time unit: apply staged isolation, at most one reboot")

    p = add("stage", cmd_stage, "write the grub.d drop-in (does not reboot)")
    p.add_argument("--cpus", help="CPU list, e.g. 8-9; chosen from topology if omitted")

    p = add("apply", cmd_apply, "pin radiod, partition L3, pin frequency")
    p.add_argument("--cpus")
    p.add_argument("--mib", type=float, help="L3 target in MiB (default 5)")
    p.add_argument("--freq-khz", type=int)
    p.add_argument("--move-irqs", action="store_true",
                   help="retarget high-rate IRQs instead of refusing")
    p.add_argument("--assume-isolated", action="store_true",
                   help="apply the R6 refusal even before isolation is active")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-freq", action="store_true")
    p.add_argument("--irq-seconds", type=float, default=2.0)

    p = add("pin", cmd_pin, "pin radiod's threads to a sibling pair")
    p.add_argument("--cpus")

    p = add("cache", cmd_cache, "partition L3 by bytes")
    p.add_argument("--mib", type=float)
    p.add_argument("--show", action="store_true", help="print geometry only")

    p = add("freq", cmd_freq, "pin frequency with min == max")
    p.add_argument("--freq-khz", type=int)
    p.add_argument("--show", action="store_true")

    p = add("irq", cmd_irq, "find high-rate interrupts on isolated cores")
    p.add_argument("--move", action="store_true")
    p.add_argument("--irq-seconds", type=float, default=2.0)

    p = add("wisdom", cmd_wisdom, "report, or converge on, FFTW ESTIMATE plans")
    p.add_argument("--plan", action="store_true",
                   help="plan what fft.log names, restart, repeat until empty")
    p.add_argument("--settle", type=float, default=20.0)

    p = add("baseline", cmd_baseline, "record this station's healthy thread figures")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--force", action="store_true",
                   help="save even though checks are failing")

    add("explain", cmd_explain, "what the three controls do, and the traps")
    return parser


def main(argv=None, out=None, environ=None, sleep=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    out = out or sys.stdout
    if not getattr(args, "func", None):
        parser.print_help(out)
        return EXIT_OK
    environ = dict(environ if environ is not None else os.environ)
    if args.dry_run:
        environ["KA9Q_TUNE_DRY_RUN"] = "1"
    return args.func(Env(environ, sleep=sleep), args, out)
