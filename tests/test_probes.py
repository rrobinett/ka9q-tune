"""R4, R6, R7, R8 and the readings underneath them."""

import unittest

import fakeroot
from ka9q_tune import (cpuset, fftw, freq as freq_mod, irq as irq_mod, procfs,
                       radiod as radiod_mod, topology as topo_mod)


class TopologyTest(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "m"):
            self.m.destroy()

    def test_sequential_sibling_pairs(self):
        self.m = fakeroot.Machine().topology(logical=12, siblings="sequential")
        t = topo_mod.Topology(self.m.env())
        self.assertEqual(t.siblings[8], frozenset({8, 9}))
        self.assertTrue(t.is_sibling_pair({8, 9}))
        self.assertFalse(t.is_sibling_pair({9, 10}))

    def test_split_sibling_pairs(self):
        # Both layouts exist in the wild; {9,10} is a pair on one and not the
        # other, so the enumeration can never be assumed.
        self.m = fakeroot.Machine().topology(logical=12, siblings="split")
        t = topo_mod.Topology(self.m.env())
        self.assertEqual(t.siblings[3], frozenset({3, 9}))
        self.assertTrue(t.is_sibling_pair({3, 9}))
        self.assertFalse(t.is_sibling_pair({8, 9}))

    def test_describe_names_the_core(self):
        self.m = fakeroot.Machine().topology(logical=12)
        t = topo_mod.Topology(self.m.env())
        self.assertEqual(t.describe({8, 9}), "core 4, sibling pair")


class PlacementTest(unittest.TestCase):
    def setUp(self):
        self.m = fakeroot.healthy()
        self.env = self.m.env()
        self.topology = topo_mod.Topology(self.env)

    def tearDown(self):
        self.m.destroy()

    def test_never_chooses_the_boot_cpu(self):
        pair, _ = radiod_mod.choose_pair(self.topology, frozenset(range(12)))
        self.assertNotIn(self.topology.boot_cpu, pair)

    def test_prefers_a_fully_isolated_pair(self):
        pair, reason = radiod_mod.choose_pair(self.topology, frozenset({8, 9}))
        self.assertEqual(pair, frozenset({8, 9}))
        self.assertIn("fully isolated", reason)

    def test_avoids_a_pair_carrying_a_high_rate_interrupt(self):
        pair, _ = radiod_mod.choose_pair(
            self.topology, frozenset({8, 9, 10, 11}), avoid=frozenset({8}))
        self.assertEqual(pair, frozenset({10, 11}))

    def test_says_so_when_only_a_compromised_pair_is_available(self):
        pair, reason = radiod_mod.choose_pair(
            self.topology, frozenset({8, 9}), avoid=frozenset({8, 9}))
        self.assertEqual(pair, frozenset({8, 9}))
        self.assertIn("high-rate interrupt", reason)

    def test_reads_affinity_from_proc(self):
        radiod = radiod_mod.Radiod(self.env, self.topology)
        self.assertEqual(radiod.process_affinity(), frozenset({8, 9}))
        self.assertEqual(radiod.unit, "radiod@WB6CXC-7")

    def test_finds_both_hot_threads(self):
        radiod = radiod_mod.Radiod(self.env, self.topology)
        self.assertEqual(radiod.tids_named("fft"), [fakeroot.FFT_TID])
        self.assertEqual(radiod.tids_named("proc_rx888"), [fakeroot.INGEST_TID])

    def test_a_process_that_merely_mentions_radiod_is_not_radiod(self):
        # A shell running a script about radiod, an editor with the config
        # open, or this tool's own command line would all match a substring
        # search -- and every reading below would then describe the wrong
        # process while looking entirely plausible.
        self.m.write("/proc/900/comm", "bash\n")
        self.m.write("/proc/900/cmdline",
                     "/bin/bash\0-c\0systemctl status radiod@WB6CXC-7\0")
        self.assertEqual(procfs.find_pid(self.env, "radiod"), fakeroot.RADIOD_PID)

    def test_matches_argv0_basename(self):
        m = fakeroot.Machine()
        try:
            m.write("/proc/700/comm", "radiod-wrapper\n")
            m.write("/proc/700/cmdline", "/usr/local/sbin/radiod\0/etc/x.conf\0")
            self.assertEqual(procfs.find_pid(m.env(), "radiod"), 700)
        finally:
            m.destroy()


class TickTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_full_tick_is_measured_not_inferred(self):
        # The ground truth. CONFIG_HZ=250 and 256.9/s is the untouched tick,
        # whatever any config file claims.
        self.m = fakeroot.broken()
        env = self.m.env()
        before = procfs.Interrupts.parse(env.read(self.m.path("/proc/interrupts")))
        self.m.advance_interrupts(10.0, loc_rate={8: 256.9, 9: 256.9})
        after = procfs.Interrupts.parse(env.read(self.m.path("/proc/interrupts")))
        ticks = procfs.tick_rates(before, after, 10.0)
        self.assertAlmostEqual(ticks[8], 256.9, delta=0.5)

    def test_tickless_core_reads_near_zero(self):
        self.m = fakeroot.healthy()
        env = self.m.env()
        before = procfs.Interrupts.parse(env.read(self.m.path("/proc/interrupts")))
        self.m.advance_interrupts(100.0, loc_rate={8: 0.4, 9: 0.4})
        after = procfs.Interrupts.parse(env.read(self.m.path("/proc/interrupts")))
        ticks = procfs.tick_rates(before, after, 100.0)
        self.assertLess(ticks[8], 1.0)

    def test_counter_going_backwards_is_not_a_negative_rate(self):
        self.m = fakeroot.healthy()
        env = self.m.env()
        after = procfs.Interrupts.parse(env.read(self.m.path("/proc/interrupts")))
        self.m.interrupts(loc={c: 2000 for c in range(12)})
        before = procfs.Interrupts.parse(env.read(self.m.path("/proc/interrupts")))
        rates = procfs.tick_rates(before, after, 10.0)
        self.assertGreaterEqual(min(rates.values()), 0.0)


class IrqTest(unittest.TestCase):
    def setUp(self):
        self.m = fakeroot.healthy()
        self.env = self.m.env()

    def tearDown(self):
        self.m.destroy()

    def _rates(self, seconds, device_rate):
        before = procfs.Interrupts.parse(self.env.read(self.m.path("/proc/interrupts")))
        self.m.advance_interrupts(seconds, device_rate=device_rate)
        after = procfs.Interrupts.parse(self.env.read(self.m.path("/proc/interrupts")))
        return (procfs.interrupt_rates(before, after, seconds), after.labels)

    def test_high_rate_irq_on_an_isolated_core_is_found(self):
        rates, labels = self._rates(5.0, {"130": {8: 1000.0}})
        findings = irq_mod.conflicts(self.env, rates, labels, {8, 9})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].cpu, 8)
        self.assertAlmostEqual(findings[0].rate, 1000.0, delta=1.0)

    def test_the_same_irq_elsewhere_is_not_a_finding(self):
        rates, labels = self._rates(5.0, {"130": {0: 1000.0, 1: 1000.0}})
        self.assertEqual(irq_mod.conflicts(self.env, rates, labels, {8, 9}), [])

    def test_delivered_counts_beat_configured_affinity(self):
        # An interrupt can be configured for cpu0 and serviced on cpu8. Only
        # the counts say where the work actually happened.
        self.m.irq("130", affinity="0-1")
        rates, labels = self._rates(5.0, {"130": {8: 900.0}})
        findings = irq_mod.conflicts(self.env, rates, labels, {8, 9})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].affinity, frozenset({0, 1}))

    def test_the_tick_itself_is_not_treated_as_a_movable_irq(self):
        before = procfs.Interrupts.parse(self.env.read(self.m.path("/proc/interrupts")))
        self.m.advance_interrupts(5.0, loc_rate={8: 250.0})
        after = procfs.Interrupts.parse(self.env.read(self.m.path("/proc/interrupts")))
        rates = procfs.interrupt_rates(before, after, 5.0)
        self.assertEqual(irq_mod.conflicts(self.env, rates, after.labels, {8, 9}), [])

    def test_retarget_writes_and_verifies(self):
        rates, labels = self._rates(5.0, {"130": {8: 1000.0}})
        finding = irq_mod.conflicts(self.env, rates, labels, {8, 9})[0]
        ok, detail = irq_mod.retarget(self.env, finding, frozenset({0, 1}))
        self.assertTrue(ok, detail)
        self.assertEqual(
            cpuset.parse(self.env.read_stripped(
                self.m.path("/proc/irq/130/smp_affinity_list"))),
            frozenset({0, 1}))

    def test_housekeeping_is_what_is_left(self):
        topology = topo_mod.Topology(self.env)
        self.assertEqual(irq_mod.housekeeping_cpus(topology, {8, 9}),
                         frozenset({0, 1, 2, 3, 4, 5, 6, 7, 10, 11}))


class FreqTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_min_equals_max_is_pinned(self):
        self.m = fakeroot.healthy()
        f = freq_mod.CpuFreq(self.m.env(), 8)
        self.assertTrue(f.pinned)

    def test_max_only_cap_is_not_pinned(self):
        # The failure R7 exists for: amd-pstate-epp parks an isolated
        # nohz_full CPU at scaling_min_freq because with the tick suppressed
        # the governor never sees load.
        self.m = fakeroot.healthy()
        self.m.cpufreq([8, 9], min_khz=400_000, max_khz=3_200_000)
        self.assertFalse(freq_mod.CpuFreq(self.m.env(), 8).pinned)

    def test_delivered_prefers_the_hardware_read(self):
        self.m = fakeroot.healthy()
        self.m.cpufreq([8], cur_khz=2_100_000, hardware_readback=True)
        khz, verified = freq_mod.CpuFreq(self.m.env(), 8).delivered()
        self.assertEqual(khz, 2_100_000)
        self.assertTrue(verified)

    def test_setpoint_echo_is_flagged_as_unverified(self):
        self.m = fakeroot.healthy()
        self.m.cpufreq([8], cur_khz=3_200_000, hardware_readback=False)
        _, verified = freq_mod.CpuFreq(self.m.env(), 8).delivered()
        self.assertFalse(verified)

    def test_set_pinned_writes_both_ends(self):
        self.m = fakeroot.healthy()
        self.m.cpufreq([8], min_khz=400_000, max_khz=1_000_000, cur_khz=3_200_000)
        f = freq_mod.CpuFreq(self.m.env(), 8)
        ok, messages = f.set_pinned(3_200_000)
        self.assertTrue(ok, messages)
        self.assertEqual(f.min_khz, 3_200_000)
        self.assertEqual(f.max_khz, 3_200_000)

    def test_raising_above_the_current_max_still_works(self):
        # Writing min above max is rejected by the kernel, so the order of the
        # three writes matters and a half-applied pin is the thing to avoid.
        self.m = fakeroot.healthy()
        self.m.cpufreq([8], min_khz=400_000, max_khz=400_000, cur_khz=400_000)
        f = freq_mod.CpuFreq(self.m.env(), 8)
        ok, _ = f.set_pinned(3_200_000)
        self.assertTrue(ok)
        self.assertTrue(f.pinned)


class WisdomTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_empty_log_means_no_estimate_plans(self):
        self.m = fakeroot.healthy()
        misses, unparsed, exists = fftw.read_log(self.m.env())
        self.assertTrue(exists)
        self.assertEqual(misses, [])
        self.assertEqual(unparsed, [])

    def test_absent_log_is_not_the_same_as_empty(self):
        self.m = fakeroot.healthy()
        self.m.remove("/var/lib/ka9q-radio/fft.log")
        _, _, exists = fftw.read_log(self.m.env())
        self.assertFalse(exists)

    def test_misses_are_extracted_and_deduplicated(self):
        self.m = fakeroot.healthy()
        self.m.fft_log(
            "no wisdom for cof1024, using FFTW_ESTIMATE\n"
            "no wisdom for cof1024, using FFTW_ESTIMATE\n"
            "no wisdom for rof2048, using FFTW_ESTIMATE\n"
        )
        misses, unparsed, _ = fftw.read_log(self.m.env())
        self.assertEqual([m.spec for m in misses], ["cof1024", "rof2048"])
        self.assertEqual(unparsed, [])

    def test_unmatched_lines_are_surfaced_not_dropped(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("something this parser has never seen\n")
        misses, unparsed, _ = fftw.read_log(self.m.env())
        self.assertEqual(misses, [])
        self.assertEqual(len(unparsed), 1)

    def test_converge_terminates_when_the_log_empties(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("miss cof1024\nmiss rof2048\n")
        calls = []

        def run(cmd):
            calls.append(cmd)
            if "wisdom" in cmd:
                return 0, ""
            return 0, ""

        ok, messages = fftw.converge(self.m.env(), run, settle_seconds=0,
                                     sleep=lambda _s: None)
        self.assertTrue(ok, messages)
        self.assertTrue(any("cof1024" in c for c in calls))

    def test_converge_stops_rather_than_looping_forever(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("miss cof1024\n")
        machine = self.m

        def run(cmd):
            if "restart" in cmd or "systemctl" in cmd:
                machine.fft_log("miss cof1024\n")   # radiod keeps missing
            return 0, ""

        ok, messages = fftw.converge(self.m.env(), run, settle_seconds=0,
                                     max_rounds=3, sleep=lambda _s: None)
        self.assertFalse(ok)
        self.assertTrue(any("stopping rather than looping" in m for m in messages))

    def test_converge_clears_the_log_so_it_does_not_read_its_own_history(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("miss cof1024\n")
        cleared = []

        def run(cmd):
            cleared.append(cmd)
            return 0, ""

        fftw.converge(self.m.env(), run, settle_seconds=0, sleep=lambda _s: None)
        with open(self.m.path("/var/lib/ka9q-radio/fft.log")) as fh:
            self.assertEqual(fh.read(), "")


class PlannerTest(unittest.TestCase):
    """radiod 2026-10-07 and later: d placement, fft-gen, version-named wisdom."""

    def tearDown(self):
        self.m.destroy()

    def test_input_destroying_transforms_are_parsed(self):
        # bc224260 logs out-of-place input-destroying transforms with a d.
        self.m = fakeroot.healthy()
        self.m.fft_log("cdb1200\nrdf640\ncof1024\nrib512\n")
        misses, unparsed, _ = fftw.read_log(self.m.env())
        self.assertEqual([m.spec for m in misses],
                         ["cdb1200", "rdf640", "cof1024", "rib512"])
        self.assertEqual(unparsed, [])

    def test_fft_gen_is_preferred_when_installed(self):
        self.m = fakeroot.healthy().fft_gen()
        self.assertEqual(fftw.planner(self.m.env()), fftw.FFT_GEN)

    def test_fftwf_wisdom_is_the_fallback(self):
        self.m = fakeroot.healthy()
        self.assertEqual(fftw.planner(self.m.env()), fftw.FFTW_WISDOM)

    def test_fft_gen_writes_where_radiod_reads(self):
        # No -o: fft-gen names the file after the FFTW build, as radiod does.
        # No -T: that would name it -threaded, which radiod does not read.
        self.m = fakeroot.healthy().fft_gen()
        cmd = fftw.plan_command(self.m.env(), ["cdb1200", "rof2048"])
        self.assertTrue(cmd.endswith("fft-gen -v cdb1200 rof2048"), cmd)
        self.assertNotIn("-T", cmd.split())
        self.assertNotIn("-o", cmd.split())

    def test_fftwf_wisdom_cannot_plan_input_destroying_transforms(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("cdb1200\ncof1024\n")
        calls = []

        def run(cmd):
            calls.append(cmd)
            return 0, ""

        ok, messages = fftw.converge(self.m.env(), run, settle_seconds=0,
                                     sleep=lambda _s: None)
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertTrue(any("cdb1200" in m and "fft-gen" in m for m in messages),
                        messages)

    def test_fft_gen_plans_input_destroying_transforms(self):
        self.m = fakeroot.healthy().fft_gen()
        self.m.fft_log("cdb1200\n")
        calls = []

        def run(cmd):
            calls.append(cmd)
            return 0, ""

        ok, messages = fftw.converge(self.m.env(), run, settle_seconds=0,
                                     sleep=lambda _s: None)
        self.assertTrue(ok, messages)
        self.assertTrue(any("fft-gen" in c and "cdb1200" in c for c in calls))


class DryRunPlanTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_dry_run_shows_the_commands_once_and_runs_nothing(self):
        self.m = fakeroot.healthy().fft_gen()
        self.m.fft_log("cdb1200\n")
        calls = []
        ok, messages = fftw.converge(
            self.m.env(DRY_RUN=1), lambda cmd: calls.append(cmd) or (0, ""),
            restart_command="systemctl restart ka9q-radio@x.service",
            settle_seconds=0, sleep=lambda _s: None)
        self.assertTrue(ok, messages)
        self.assertEqual(calls, [])
        text = "\n".join(messages)
        self.assertIn("fft-gen -v cdb1200", text)
        self.assertIn("ka9q-radio@x.service", text)
        self.assertEqual(text.count("round "), 1, text)
        with open(self.m.path("/var/lib/ka9q-radio/fft.log")) as fh:
            self.assertEqual(fh.read(), "cdb1200\n")


class RestartTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_packaged_unit_is_read_from_the_cgroup(self):
        # The command line names /etc/radio/devices/04b4-00f1.conf, which the
        # fallback would turn into radiod@04b4-00f1 -- a unit that is not there.
        self.m = fakeroot.healthy()
        self.m.radiod(unit="ka9q-radio@04b4-00f1")
        env = self.m.env()
        r = radiod_mod.Radiod(env, topo_mod.Topology(env))
        self.assertEqual(r.unit, "ka9q-radio@04b4-00f1")

    def test_restart_names_the_running_unit(self):
        self.m = fakeroot.healthy()
        self.assertEqual(
            fftw.restart_command(self.m.env(), "ka9q-radio@04b4-00f1"),
            "systemctl restart ka9q-radio@04b4-00f1.service")

    def test_an_explicit_restart_command_wins(self):
        self.m = fakeroot.healthy()
        env = self.m.env(RADIOD_RESTART="my-restart")
        self.assertEqual(fftw.restart_command(env, "ka9q-radio@x"), "my-restart")

    def test_no_unit_falls_back_to_the_default(self):
        self.m = fakeroot.healthy()
        env = self.m.env()
        self.assertEqual(fftw.restart_command(env, None),
                         env.command("RADIOD_RESTART"))


class StaleLogTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_process_start_is_btime_plus_starttime(self):
        self.m = fakeroot.healthy()
        self.m.radiod(started=fakeroot.BOOT_TIME + 3000)
        self.assertEqual(procfs.process_start(self.m.env(), fakeroot.RADIOD_PID),
                         fakeroot.BOOT_TIME + 3000)

    def test_a_log_older_than_radiod_predates_it(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("cof1024\n", mtime=fakeroot.BOOT_TIME + 10)
        self.assertTrue(fftw.log_predates(self.m.env(), fakeroot.BOOT_TIME + 60))

    def test_a_log_written_since_radiod_started_does_not(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("cof1024\n", mtime=fakeroot.BOOT_TIME + 120)
        self.assertFalse(fftw.log_predates(self.m.env(), fakeroot.BOOT_TIME + 60))

    def test_unknown_start_is_not_called_stale(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("cof1024\n", mtime=fakeroot.BOOT_TIME + 10)
        self.assertIsNone(fftw.log_predates(self.m.env(), None))


class StatParsingTest(unittest.TestCase):
    def test_comm_with_spaces_and_parens(self):
        # Splitting the line on whitespace puts utime in the wrong column for
        # any thread whose name contains a space.
        # After the final ')': state, ppid, and nine more fields, then utime
        # and stime -- fields 14 and 15 of the line as a whole.
        line = "123 (my thread (x)) S 1 " + " ".join(["0"] * 9) + " 500 250 " \
               + " ".join(["0"] * 30)
        comm, utime, stime = procfs.parse_stat(line)
        self.assertEqual(comm, "my thread (x)")
        self.assertEqual((utime, stime), (500, 250))

    def test_percentages(self):
        before = procfs.ThreadSample({1: 0}, {1: "fft"})
        after = procfs.ThreadSample({1: 1443}, {1: "fft"})
        pct = procfs.thread_percentages(before, after, 30.0, 100)
        self.assertAlmostEqual(pct[1], 48.1, delta=0.1)


if __name__ == "__main__":
    unittest.main()
