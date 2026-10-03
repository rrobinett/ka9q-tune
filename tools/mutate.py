#!/usr/bin/env python3
"""Measure the suite's power by breaking the fixes and watching it fail.

A test nobody has watched fail is not a test. This applies each mutation
below -- every one of them a plausible way to write this package wrong, and
most of them the way it was actually written wrong somewhere -- runs the full
suite against the mutated source, and reports a mutation that SURVIVED as a
hole in the suite.

Run:  python3 tools/mutate.py [-v]

Exit 0 when every mutation was caught.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Mutation:
    def __init__(self, name, filename, edits, why):
        self.name = name
        self.filename = filename
        self.edits = edits          # [(old, new)] applied in order
        self.why = why

    def apply(self, root):
        path = os.path.join(root, "ka9q_tune", self.filename)
        with open(path) as fh:
            text = fh.read()
        for old, new in self.edits:
            if old not in text:
                raise AssertionError(
                    "mutation %r no longer matches %s: the code it targets has "
                    "moved, so this mutation is testing nothing"
                    % (self.name, self.filename))
            text = text.replace(old, new, 1)
        with open(path, "w") as fh:
            fh.write(text)


MUTATIONS = [
    Mutation(
        "R1: read the kernel command line instead of sysfs",
        "isolation.py",
        [('    nohz = env.read_stripped(env.sys_cpu("nohz_full"), None)',
          '    nohz = procfs.cmdline_params(procfs.kernel_cmdline(env)).get("nohz_full")')],
        "The exact bug that reports a station with cpu0 still ticking as healthy.",
    ),
    Mutation(
        "R1: trust the rcu_nocbs parameter rather than the rcuo kthreads",
        "isolation.py",
        [('        "rcu_nocbs": procfs.nocb_cpus(env),',
          '        "rcu_nocbs": cpuset.parse(procfs.cmdline_params('
          'procfs.kernel_cmdline(env)).get("rcu_nocbs")),')],
        "Without the kthreads the tick cannot stop, whatever was requested.",
    ),
    Mutation(
        "R2: ignore the drop-in's mtime against boot time",
        "isolation.py",
        [("    return mtime > booted", "    return False")],
        "This is the sixteen-hour fault: staged after boot, never loaded.",
    ),
    Mutation(
        "R2: compare CPU lists as strings, not sets",
        "cpuset.py",
        [("    return frozenset(out)", "    return frozenset([text])")],
        "12-13 and 12,13 become different, and the station reboots forever.",
    ),
    Mutation(
        "R3: write the marker AFTER the reboot instead of before",
        "isolation.py",
        [("    ok, detail = write_marker(env, state.staged)\n"
          "    messages.append(detail)",
          '    ok, detail = True, "marker deferred"\n'
          "    messages.append(detail)"),
         ('    return OneShotResult(EXIT_REBOOT_INVOKED, "reboot-invoked", messages)',
          "    write_marker(env, state.staged)\n"
          '    return OneShotResult(EXIT_REBOOT_INVOKED, "reboot-invoked", messages)')],
        "A kill between the two turns one reboot into a loop.",
    ),
    Mutation(
        "R3: retry forever on a kernel without CONFIG_NO_HZ_FULL",
        "isolation.py",
        [("    if not state.supported:\n"
          "        messages.append(",
          "    if False:\n"
          "        messages.append(")],
        "A reboot loop that buries the cause and takes the station offline.",
    ),
    Mutation(
        "R3: let a marker from an older configuration block a new one",
        "isolation.py",
        [("    for param in PARAMS:\n"
          '        if cpuset.parse(marker["staged"].get(param, "")) != staged[param]:\n'
          "            return False\n"
          "    return True",
          "    return True")],
        "An operator fixes the drop-in and the fix is never applied.",
    ),
    Mutation(
        "R3: never rearm the one-shot after a good boot",
        "isolation.py",
        [("        clear_marker(env)\n", "")],
        "R2's every-boot check silently becomes a once-ever check.",
    ),
    Mutation(
        "R4: allow the boot CPU to be chosen",
        "radiod.py",
        [("        if boot in pair:\n            continue\n", "")],
        "Half the sibling pair keeps ticking whatever the command line says.",
    ),
    Mutation(
        "R4: assume sequential hyperthread enumeration",
        "topology.py",
        [('        sibs = cpuset.parse(self.env.read_stripped('
          'os.path.join(base, "thread_siblings_list"), ""))',
          "        sibs = frozenset([cpu - cpu % 2, cpu - cpu % 2 + 1])")],
        "Split layouts ({0,8},{1,9}) exist in the wild and this gets them wrong.",
    ),
    Mutation(
        "R5: configure a fixed ten-way mask instead of a byte target",
        "cache.py",
        [("        ways = int(-(-target_bytes // int(per_way)))  # ceil",
          "        ways = 10")],
        "Ten ways is 10 MiB on one part and 5 MiB on another.",
    ),
    Mutation(
        "R5: let the allocation overlap the default group",
        "cache.py",
        [("        if exclusive:", "        if False:")],
        "A group sharing its ways with everything else is not a partition.",
    ),
    Mutation(
        "R5: report success without reading the size back",
        "cache.py",
        [("            if size < target_bytes:",
          "            if False:")],
        "Never report success from configuration.",
    ),
    Mutation(
        "R6: warn about a co-located high-rate IRQ instead of refusing",
        "cli.py",
        [("            return EXIT_REFUSED\n"
          "        for finding in findings:\n"
          "            ok, detail = irq_mod.retarget(env, finding, housekeeping)",
          "        for finding in findings:\n"
          "            ok, detail = irq_mod.retarget(env, finding, housekeeping)")],
        "R6 says detect and refuse. 20.82 gaps per channel-hour against 0.68.",
    ),
    Mutation(
        "R6: trust smp_affinity instead of the delivered counts",
        "irq.py",
        [("            if cpu in cpus and rate >= threshold:",
          "            if False and rate >= threshold:")],
        "An IRQ configured for cpu0 can be serviced on cpu8.",
    ),
    Mutation(
        "R7: set a max-only cap",
        "freq.py",
        [('            ("scaling_min_freq", khz),\n        ):', "        ):")],
        "amd-pstate-epp then parks the isolated CPU at scaling_min_freq.",
    ),
    Mutation(
        "R7: accept scaling_cur_freq as a verified reading",
        "freq.py",
        [("        return self._int(\"scaling_cur_freq\"), False",
          "        return self._int(\"scaling_cur_freq\"), True")],
        "On some drivers it echoes the setpoint that was just written.",
    ),
    Mutation(
        "R8: do not clear fft.log between planning rounds",
        "fftw.py",
        [("        ok, detail = clear_log(env)\n        messages.append(detail)\n"
          "        if not ok:\n            return False, messages",
          "        ok = True")],
        "The planner then reads its own history and never converges.",
    ),
    Mutation(
        "R8: treat a missing fft.log as an empty one",
        "fftw.py",
        [("    if raw is None:\n        return [], [], False",
          "    if raw is None:\n        return [], [], True")],
        "Absent is not empty, and the difference is the whole signal.",
    ),
    Mutation(
        "discriminator: call a joint elevation a transform problem",
        "diagnose.py",
        [("            return Verdict(\n                CORE,",
          "            return Verdict(\n                PLANS,")],
        "This is the walked-past-three-times mistake the package exists for.",
    ),
    Mutation(
        "ground truth: stop treating a full tick as a fault",
        "report.py",
        [("        if rate >= 50.0:\n            worst = BAD",
          "        if False:\n            worst = BAD")],
        "The counter is the only thing that knows the tick is still firing.",
    ),
    Mutation(
        "identity: match radiod by any substring of the command line",
        "procfs.py",
        [("        if comm == pattern or argv0 == pattern:",
          "        if pattern in comm or pattern in ' '.join(argv):")],
        "Picks up a shell that merely mentions radiod, and every reading then "
        "describes the wrong process while looking plausible.",
    ),
    Mutation(
        "silence: drop the staged-not-applied line from the report",
        "report.py",
        [('    status.add("  staged", staged + "   STAGED, NOT APPLIED", BAD, notes)',
          '    status.add("  staged", staged, OK, notes)')],
        "Failing silently is the defect, not the mitigation.",
    ),
]


def run_suite(root, verbose=False):
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + os.path.join(root, "tests")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s",
         os.path.join(root, "tests"), "-p", "test_*.py"],
        cwd=root, capture_output=True, text=True, env=env, timeout=900,
    )
    if verbose and proc.returncode == 0:
        sys.stderr.write(proc.stdout + proc.stderr)
    return proc.returncode, (proc.stdout + proc.stderr)


def failing_tests(output):
    return sorted({line.split("(")[0].strip()[len("FAIL: "):]
                   for line in output.splitlines()
                   if line.startswith(("FAIL: ", "ERROR: "))})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-k", "--filter", help="only mutations whose name matches")
    args = parser.parse_args()

    print("baseline: ", end="", flush=True)
    code, output = run_suite(ROOT)
    if code != 0:
        print("FAIL -- the unmutated suite does not pass; fix that first")
        print(output[-4000:])
        return 2
    print("pass")

    survivors = []
    selected = [m for m in MUTATIONS
                if not args.filter or args.filter.lower() in m.name.lower()]
    for mutation in selected:
        workdir = tempfile.mkdtemp(prefix="ka9q-mutate-")
        try:
            for name in ("ka9q_tune", "tests"):
                shutil.copytree(os.path.join(ROOT, name),
                                os.path.join(workdir, name))
            mutation.apply(workdir)
            code, output = run_suite(workdir)
            caught = code != 0
            print("%-6s %s" % ("caught" if caught else "SURVIVED", mutation.name))
            if caught and args.verbose:
                for name in failing_tests(output)[:6]:
                    print("         %s" % name)
            if not caught:
                survivors.append(mutation)
                print("         %s" % mutation.why)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    print()
    if survivors:
        print("%d of %d mutations survived -- the suite does not cover them:"
              % (len(survivors), len(selected)))
        for mutation in survivors:
            print("  %s" % mutation.name)
        return 1
    print("all %d mutations caught" % len(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
