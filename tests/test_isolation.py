"""R1 and R2: delivered state, and the staged/applied gap."""

import unittest

import fakeroot
from ka9q_tune import cpuset, isolation


class DeliveredStateTest(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "m"):
            self.m.destroy()

    def test_healthy_station_reports_active(self):
        self.m = fakeroot.healthy()
        state = isolation.State(self.m.env())
        self.assertEqual(state.delivered["nohz_full"], frozenset({8, 9}))
        self.assertEqual(state.delivered["rcu_nocbs"], frozenset({8, 9}))
        self.assertEqual(state.effective_isolated(), frozenset({8, 9}))
        self.assertTrue(state.staged_is_active())

    def test_broken_station_reports_nothing_delivered(self):
        self.m = fakeroot.broken()
        state = isolation.State(self.m.env())
        self.assertEqual(state.effective_isolated(), frozenset())
        self.assertFalse(state.staged_is_active())
        self.assertEqual(state.gap()["nohz_full"], frozenset({8, 9}))

    def test_sysfs_wins_over_cmdline(self):
        # The trap: the boot CPU can never be nohz_full. A command line reading
        # nohz_full=0-9 yields a sysfs file reading 1-9, and a tool that reads
        # /proc/cmdline reports success on a machine where cpu0 keeps ticking.
        self.m = fakeroot.healthy()
        self.m.cmdline("quiet isolcpus=0-9 nohz_full=0-9 rcu_nocbs=0-9")
        self.m.nohz_full("1-9")
        state = isolation.State(self.m.env())
        self.assertEqual(state.requested["nohz_full"], frozenset(range(0, 10)))
        self.assertEqual(state.delivered["nohz_full"], frozenset(range(1, 10)))
        self.assertEqual(state.dropped_by_kernel, frozenset({0}))

    def test_rcu_nocbs_comes_from_kthreads_not_cmdline(self):
        # There is no sysfs file for rcu_nocbs. A command line that asks for it
        # on a kernel that cannot provide it leaves no rcuop threads, and the
        # tick cannot stop without them.
        self.m = fakeroot.healthy()
        self.m.write("/proc/400/comm", "kworker/0:1\n")
        self.m.write("/proc/401/comm", "kworker/0:2\n")
        state = isolation.State(self.m.env())
        self.assertEqual(state.delivered["rcu_nocbs"], frozenset())
        self.assertEqual(state.effective_isolated(), frozenset())

    def test_isolcpus_flag_prefix_is_not_parsed_as_cpu_numbers(self):
        self.m = fakeroot.healthy()
        self.m.cmdline("quiet isolcpus=managed_irq,domain,8-9 nohz_full=8-9")
        state = isolation.State(self.m.env())
        self.assertEqual(state.requested["isolcpus"], frozenset({8, 9}))

    def test_partial_isolation_is_not_active(self):
        # The three only work as a set. nohz_full without rcu_nocbs cannot stop
        # the tick at all, so reporting it as isolation would be a lie.
        self.m = fakeroot.healthy()
        self.m.rcu_offload([])
        for pid in (400, 401):
            self.m.remove("/proc/%d/comm" % pid)
        state = isolation.State(self.m.env())
        self.assertEqual(state.delivered["nohz_full"], frozenset({8, 9}))
        self.assertEqual(state.effective_isolated(), frozenset())


class StagedTest(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "m"):
            self.m.destroy()

    def test_dropin_written_after_boot_is_staged_not_applied(self):
        # The sixteen-hour fault: a correct drop-in, a correctly rebuilt
        # grub.cfg, and a kernel booted 41 minutes before the drop-in existed.
        self.m = fakeroot.broken()
        env = self.m.env()
        self.assertTrue(isolation.staged_after_boot(env))
        state = isolation.State(env)
        self.assertTrue(state.staged_anything)
        self.assertFalse(state.staged_is_active())

    def test_dropin_written_before_boot_and_delivered_is_applied(self):
        self.m = fakeroot.healthy()
        env = self.m.env()
        self.assertFalse(isolation.staged_after_boot(env))
        self.assertTrue(isolation.State(env).staged_is_active())

    def test_staged_range_matches_delivered_list(self):
        # 12-13 staged, 12,13 delivered: the same machine state.
        self.m = fakeroot.healthy()
        self.m.topology(logical=16)
        self.m.dropin("12-13", mtime=fakeroot.BOOT_TIME - 60)
        self.m.nohz_full("12,13").isolated("12,13").rcu_offload([12, 13])
        self.assertTrue(isolation.State(self.m.env()).staged_is_active())

    def test_missing_dropin_stages_nothing(self):
        self.m = fakeroot.healthy()
        self.m.remove("/etc/default/grub.d/99-ka9q-isolation.cfg")
        state = isolation.State(self.m.env())
        self.assertFalse(state.staged_anything)

    def test_grub_cfg_without_the_parameters_is_detected(self):
        self.m = fakeroot.broken()
        self.m.grub_cfg(text="menuentry 'Debian' {\n  linux /vmlinuz ro quiet\n}\n")
        state = isolation.State(self.m.env())
        self.assertFalse(isolation.grub_cfg_has(self.m.env(), state.staged))

    def test_grub_cfg_with_the_parameters_is_detected(self):
        self.m = fakeroot.broken()
        state = isolation.State(self.m.env())
        self.assertTrue(isolation.grub_cfg_has(self.m.env(), state.staged))


class ParamParsingTest(unittest.TestCase):
    def test_params_from_cmdline(self):
        params = isolation.params_from_cmdline(
            "ro quiet isolcpus=8-9 nohz_full=8,9 rcu_nocbs=8-9 splash")
        self.assertEqual(params["isolcpus"], frozenset({8, 9}))
        self.assertEqual(params["nohz_full"], frozenset({8, 9}))

    def test_dropin_interpolation_is_stripped(self):
        env = fakeroot.Machine()
        try:
            env.dropin("8-9")
            staged, _ = isolation.read_staged(env.env())
            self.assertEqual(staged["nohz_full"], frozenset({8, 9}))
            self.assertEqual(cpuset.format(staged["isolcpus"]), "8,9")
        finally:
            env.destroy()


if __name__ == "__main__":
    unittest.main()
