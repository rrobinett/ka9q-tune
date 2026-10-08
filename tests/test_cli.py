"""End to end: the report an operator sees, and the refusals.

The sampling window is driven by an injected sleep that advances the fixture
machine instead of waiting. That is deliberate: it means these tests exercise
the real two-snapshot rate arithmetic rather than a stubbed number, so a bug
in the differencing shows up here.
"""

import io
import unittest
from unittest import mock

import fakeroot
from ka9q_tune import cli, report


def run(machine, argv, sleep=None, **env_overrides):
    out = io.StringIO()
    environ = {"KA9Q_TUNE_ROOT": machine.root, "KA9Q_TUNE_CLK_TCK": "100",
               "KA9Q_TUNE_CONFIG_HZ": str(fakeroot.CONFIG_HZ)}
    for key, value in env_overrides.items():
        environ["KA9Q_TUNE_" + key] = str(value)
    code = cli.main(argv, out=out, environ=environ,
                    sleep=sleep or (lambda _seconds: None))
    return code, out.getvalue()


class StationCase(unittest.TestCase):
    """A fixture whose clock only advances when the code under test sleeps."""

    tick_rate = 0.4
    fft_rate = 48.1          # percent of one core, == ticks/s at CLK_TCK 100
    ingest_rate = 22.0
    device_irq = {"130": {0: 5000.0}}

    def advance(self, seconds):
        if seconds <= 0:
            return
        self.m.advance_interrupts(
            seconds,
            loc_rate={c: self.tick_rate for c in range(12)},
            device_rate=self.device_irq,
        )
        self.m.advance_threads(fakeroot.RADIOD_PID, {
            fakeroot.FFT_TID: round(self.fft_rate * seconds),
            fakeroot.INGEST_TID: round(self.ingest_rate * seconds),
        })

    def status(self, seconds=30.0):
        return report.collect(self.m.env(sleep=self.advance), seconds=seconds)

    def tearDown(self):
        self.m.destroy()


class HealthyStationTest(StationCase):
    def setUp(self):
        self.m = fakeroot.healthy()

    def test_status_reports_every_condition_delivered(self):
        status = self.status()
        text = report.render(status)
        self.assertEqual(status.worst, report.OK, text)
        self.assertIn("radiod@WB6CXC-7", text)
        self.assertIn("core 4, sibling pair", text)
        self.assertIn("nohz_full=8,9", text)
        self.assertIn("past the knee", text)
        self.assertIn("no ESTIMATE plans", text)

    def test_status_shows_both_hot_threads_side_by_side(self):
        text = report.render(self.status())
        self.assertIn("fft 48.1%", text)
        self.assertIn("proc_rx888 22.0%", text)
        self.assertIn("both scaling together", text)

    def test_tick_is_measured_at_near_zero(self):
        text = report.render(self.status())
        self.assertIn("0.4/s on cpu8", text)

    def test_check_exits_zero(self):
        code, text = run(self.m, ["check", "--seconds", "10"],
                         sleep=self.advance)
        self.assertEqual(code, 0, text)


class TickingDespiteSysfsTest(StationCase):
    """Isolation delivered on paper, and the tick still firing.

    nohz_full only stops the tick when exactly one task is runnable on the
    CPU. With two, the kernel needs the tick to preempt between them and
    brings it straight back -- so sysfs can report everything correct while
    the core is paying the full tick. This is why the interrupt counter is the
    ground truth and the sysfs file is only a fact about configuration.
    """

    tick_rate = 256.9
    fft_rate = 94.2
    ingest_rate = 41.6

    def setUp(self):
        self.m = fakeroot.healthy()

    def test_the_tick_alone_makes_the_report_bad(self):
        status = self.status()
        states = {line.label.strip(): line.state for line in status.lines}
        self.assertEqual(states["isolation"], report.OK)
        self.assertEqual(states["tick"], report.BAD)
        self.assertEqual(status.worst, report.BAD)
        self.assertIn("256.9/s", report.render(status))


class BrokenStationTest(StationCase):
    # WB6CXC-7 before the fix. The two rows differ by one kernel command line.
    tick_rate = 256.9
    fft_rate = 94.2
    ingest_rate = 41.6

    def setUp(self):
        self.m = fakeroot.broken()
        self.m.resctrl(group="radiod", group_mask="3ff")

    def test_status_is_unmistakable(self):
        status = self.status()
        text = report.render(status)
        self.assertEqual(status.worst, report.BAD)
        self.assertIn("NOT ACTIVE", text)
        self.assertIn("256.9/s", text)
        self.assertIn("STAGED, NOT APPLIED", text)
        self.assertIn("AFTER the running kernel booted", text)

    def test_status_names_the_two_thread_figures_and_the_core(self):
        text = report.render(self.status())
        self.assertIn("fft 94.2%", text)
        self.assertIn("proc_rx888 41.6%", text)
        self.assertIn("CORE", text)

    def test_the_bad_station_still_shows_every_unit_green_elsewhere(self):
        # The point of the whole package: nothing else reports this. L3 is
        # partitioned, the frequency is pinned, fft.log is empty, and the
        # station is unusable.
        status = self.status()
        states = {line.label.strip(): line.state for line in status.lines}
        self.assertEqual(states["L3 partition"], report.OK)
        self.assertEqual(states["frequency"], report.OK)
        self.assertEqual(states["fftw wisdom"], report.OK)
        self.assertEqual(states["isolation"], report.BAD)

    def test_check_exits_nonzero_and_names_the_problems(self):
        code, text = run(self.m, ["check", "--seconds", "10"], sleep=self.advance)
        self.assertEqual(code, 2)
        self.assertIn("isolation", text)


class RefusalTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def _irq_on(self, cpu, rate=1000.0):
        def advance(seconds):
            if seconds > 0:
                self.m.advance_interrupts(seconds, device_rate={"130": {cpu: rate}})
        return advance

    def test_pinning_to_the_boot_cpu_is_refused(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "pin", "--cpus", "0-1"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("boot CPU", text)

    def test_a_non_sibling_pair_warns_but_proceeds(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "pin", "--cpus", "9-10"])
        self.assertIn("not a hyperthread sibling pair", text)
        self.assertEqual(code, cli.EXIT_OK)

    def test_apply_refuses_a_nohz_full_core_with_a_co_located_irq(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "apply", "--cpus", "8-9",
                                  "--irq-seconds", "2"],
                         sleep=self._irq_on(8))
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("REFUSED", text)
        self.assertIn("20.82", text)

    def test_the_same_irq_on_a_housekeeping_cpu_is_not_refused(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "apply", "--cpus", "8-9",
                                  "--irq-seconds", "2"],
                         sleep=self._irq_on(0))
        self.assertNotIn("REFUSED", text)

    def test_move_irqs_turns_the_refusal_into_a_fix(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "apply", "--cpus", "8-9",
                                  "--irq-seconds", "2", "--move-irqs"],
                         sleep=self._irq_on(8))
        self.assertIn("moved to", text)
        self.assertNotIn("REFUSED", text)

    def test_stage_does_not_reboot(self):
        self.m = fakeroot.broken()
        code, text = run(self.m, ["stage", "--cpus", "8-9"], GRUB_UPDATE="true")
        self.assertEqual(code, cli.EXIT_REBOOT_REQUIRED)
        self.assertIn("does not reboot", text)
        self.assertIn("NOT APPLIED", text)

    def test_apply_on_an_unisolated_station_says_a_reboot_is_needed(self):
        self.m = fakeroot.broken()
        code, text = run(self.m, ["--dry-run", "apply", "--cpus", "8-9",
                                  "--irq-seconds", "0"])
        self.assertEqual(code, cli.EXIT_REBOOT_REQUIRED)
        self.assertIn("NOT fully isolated", text)

    def test_baseline_refuses_to_record_a_broken_station(self):
        self.m = fakeroot.broken()
        code, text = run(self.m, ["baseline", "--seconds", "1"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("encode the fault as normal", text)


class DryRunTest(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        m = fakeroot.broken()
        try:
            dropin = m.path("/etc/default/grub.d/99-ka9q-isolation.cfg")
            with open(dropin) as fh:
                before = fh.read()
            run(m, ["--dry-run", "stage", "--cpus", "10-11"], GRUB_UPDATE="true")
            with open(dropin) as fh:
                self.assertEqual(fh.read(), before)
        finally:
            m.destroy()


class OneShotCliTest(unittest.TestCase):
    def test_exit_codes_reach_the_shell(self):
        m = fakeroot.broken()
        try:
            code, _ = run(m, ["isolate-oneshot"], REBOOT="true", GRUB_UPDATE="true")
            self.assertEqual(code, 10)
            code, text = run(m, ["isolate-oneshot"], REBOOT="true",
                             GRUB_UPDATE="true")
            self.assertEqual(code, 20)
            self.assertIn("reboot-spent", text)
        finally:
            m.destroy()

    def test_healthy_station_exits_zero(self):
        m = fakeroot.healthy()
        try:
            code, text = run(m, ["isolate-oneshot"], REBOOT="false",
                             GRUB_UPDATE="false")
            self.assertEqual(code, 0, text)
        finally:
            m.destroy()


class WisdomTest(StationCase):
    """fft.log on a station whose radiod started at BOOT_TIME + 60."""

    def setUp(self):
        self.m = fakeroot.healthy()
        self.m.fft_log("cdb1200\nrof3240000\n")

    def wisdom_line(self):
        status = self.status(seconds=1.0)
        return [l for l in status.lines if l.label == "fftw wisdom"][0]

    def test_misses_logged_by_this_radiod_are_bad(self):
        self.m.fft_log("cdb1200\nrof3240000\n", mtime=fakeroot.BOOT_TIME + 120)
        line = self.wisdom_line()
        self.assertEqual(line.state, report.BAD)
        self.assertIn("2 transform(s) on ESTIMATE plans", line.value)

    def test_a_log_older_than_radiod_is_stale_not_bad(self):
        self.m.fft_log("cdb1200\nrof3240000\n", mtime=fakeroot.BOOT_TIME + 10)
        line = self.wisdom_line()
        self.assertEqual(line.state, report.WARN)
        self.assertIn("stale", line.value)
        self.assertTrue(any("before this radiod started" in n for n in line.notes))

    def test_wisdom_command_says_stale_and_exits_warn(self):
        self.m.fft_log("cdb1200\n", mtime=fakeroot.BOOT_TIME + 10)
        code, text = run(self.m, ["wisdom"])
        self.assertEqual(code, cli.EXIT_WARN, text)
        self.assertIn("predates this radiod", text)
        self.assertNotIn("unparsed", text)

    def test_plan_waits_for_the_planner_and_restarts_the_running_unit(self):
        self.m.fft_gen().radiod(unit="ka9q-radio@04b4-00f1")
        calls = []

        def fake_run(env, cmd, timeout=600):
            calls.append((cmd, timeout))
            return 0, ""

        with mock.patch("ka9q_tune.isolation.run_command", fake_run):
            code, text = run(self.m, ["wisdom", "--plan", "--settle", "0"],
                             FFT_GEN=self.m.path(fakeroot.FFT_GEN))
        self.assertEqual(code, cli.EXIT_OK, text)
        plan = [c for c in calls if "fft-gen" in c[0]]
        self.assertTrue(plan, calls)
        self.assertIsNone(plan[0][1], "planning must not be cut off at 600 s")
        self.assertIn(("systemctl restart ka9q-radio@04b4-00f1.service", None),
                      calls)


class ExplainTest(unittest.TestCase):
    def test_explain_states_the_mechanism_and_its_limits(self):
        m = fakeroot.healthy()
        try:
            code, text = run(m, ["explain"])
            self.assertEqual(code, 0)
            self.assertIn("only stops the tick when exactly ONE runnable task", text)
            self.assertIn("129.6 Msps", text)
            self.assertIn("narrowband", text)
            self.assertIn("BOTH sides", text)
        finally:
            m.destroy()


if __name__ == "__main__":
    unittest.main()
