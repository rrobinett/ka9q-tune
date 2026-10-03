"""R3: at most one reboot, marker written before it, stuck states named.

Every test here drives the real one_shot() with a reboot command that records
rather than reboots, so the ordering guarantees are actually exercised.
"""

import json
import os
import unittest

import fakeroot
from ka9q_tune import isolation


class Recorder:
    """A reboot command that writes a file, so ordering can be asserted."""

    def __init__(self, machine, marker_path, fail=False):
        self.machine = machine
        self.marker_path = marker_path
        self.fail = fail
        self.marker_existed_at_reboot = None

    def command(self):
        # The shell command the one-shot will run. It records whether the
        # marker was already on disk at the moment the reboot was invoked.
        script = (
            'if [ -e "%s" ]; then echo yes > "%s/reboot-saw-marker"; '
            'else echo no > "%s/reboot-saw-marker"; fi; exit %d'
            % (self.marker_path, self.machine.root, self.machine.root,
               1 if self.fail else 0)
        )
        return script

    def saw_marker(self):
        path = os.path.join(self.machine.root, "reboot-saw-marker")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return fh.read().strip() == "yes"


class OneShotTest(unittest.TestCase):
    def setUp(self):
        self.m = fakeroot.broken()
        self.marker = self.m.path("/var/lib/ka9q-tune/isolation-reboot.marker")
        self.recorder = Recorder(self.m, self.marker)

    def tearDown(self):
        self.m.destroy()

    def env(self, **kw):
        return self.m.env(REBOOT=self.recorder.command(),
                          GRUB_UPDATE="true", **kw)

    def test_staged_and_not_active_reboots_once(self):
        result = isolation.one_shot(self.env())
        self.assertEqual(result.code, isolation.EXIT_REBOOT_INVOKED)
        self.assertEqual(result.status, "reboot-invoked")
        self.assertTrue(os.path.exists(self.marker))

    def test_marker_is_written_before_the_reboot(self):
        # If the marker went down after the reboot call, a process killed in
        # between would come back and reboot again, and again.
        isolation.one_shot(self.env())
        self.assertIs(self.recorder.saw_marker(), True)

    def test_second_run_does_not_reboot_again(self):
        env = self.env()
        self.assertEqual(isolation.one_shot(env).code,
                         isolation.EXIT_REBOOT_INVOKED)
        second = isolation.one_shot(env)
        self.assertEqual(second.code, isolation.EXIT_STAGED_NOT_ACTIVE)
        self.assertEqual(second.status, "reboot-spent")

    def test_already_active_does_nothing_and_clears_the_marker(self):
        healthy = fakeroot.healthy()
        try:
            marker = healthy.path("/var/lib/ka9q-tune/isolation-reboot.marker")
            healthy.write("/var/lib/ka9q-tune/isolation-reboot.marker", "{}")
            env = healthy.env(REBOOT="false", GRUB_UPDATE="true")
            result = isolation.one_shot(env)
            self.assertEqual(result.code, isolation.EXIT_NOTHING_TO_DO)
            self.assertEqual(result.status, "active")
            # Rearming on a good boot is what makes R2's "every boot, not once
            # at install" work: a later regression gets its own single reboot.
            self.assertFalse(os.path.exists(marker))
        finally:
            healthy.destroy()

    def test_nothing_staged_does_nothing(self):
        self.m.remove("/etc/default/grub.d/99-ka9q-isolation.cfg")
        result = isolation.one_shot(self.env())
        self.assertEqual(result.code, isolation.EXIT_NOTHING_TO_DO)
        self.assertEqual(result.status, "nothing-staged")

    def test_kernel_without_nohz_full_support_never_reboots(self):
        # No number of reboots fixes a kernel built without CONFIG_NO_HZ_FULL,
        # so this must be a named stuck state, not a retry.
        self.m.nohz_full(None)
        result = isolation.one_shot(self.env())
        self.assertEqual(result.code, isolation.EXIT_STAGED_NOT_ACTIVE)
        self.assertEqual(result.status, "unsupported-kernel")
        self.assertFalse(os.path.exists(self.marker))

    def test_grub_update_failure_is_a_named_stuck_state(self):
        self.m.grub_cfg(text="menuentry 'Debian' {\n  linux /vmlinuz ro\n}\n")
        result = isolation.one_shot(self.m.env(REBOOT=self.recorder.command(),
                                               GRUB_UPDATE="false"))
        self.assertEqual(result.code, isolation.EXIT_STAGED_NOT_ACTIVE)
        self.assertEqual(result.status, "grub-update-failed")
        self.assertFalse(os.path.exists(self.marker))

    def test_grub_cfg_still_stale_after_update_is_a_named_stuck_state(self):
        self.m.grub_cfg(text="menuentry 'Debian' {\n  linux /vmlinuz ro\n}\n")
        result = isolation.one_shot(self.m.env(REBOOT=self.recorder.command(),
                                               GRUB_UPDATE="true"))
        self.assertEqual(result.code, isolation.EXIT_STAGED_NOT_ACTIVE)
        self.assertEqual(result.status, "grub-cfg-stale")

    def test_failed_reboot_command_is_reported_not_retried(self):
        failing = Recorder(self.m, self.marker, fail=True)
        result = isolation.one_shot(self.m.env(REBOOT=failing.command(),
                                               GRUB_UPDATE="true"))
        self.assertEqual(result.code, isolation.EXIT_STAGED_NOT_ACTIVE)
        self.assertEqual(result.status, "reboot-failed")

    def test_a_new_staged_configuration_earns_a_new_reboot(self):
        # Otherwise an operator who corrects the drop-in finds the correction
        # permanently blocked by a marker written for the old one.
        env = self.env()
        isolation.one_shot(env)
        self.m.dropin("10-11", mtime=fakeroot.BOOT_TIME + 3600)
        self.m.grub_cfg(cpus="10-11")
        result = isolation.one_shot(env)
        self.assertEqual(result.code, isolation.EXIT_REBOOT_INVOKED)

    def test_marker_records_the_configuration_it_rebooted_for(self):
        isolation.one_shot(self.env())
        with open(self.marker) as fh:
            payload = json.load(fh)
        self.assertEqual(payload["staged"]["nohz_full"], "8,9")

    def test_unparseable_marker_still_blocks_a_second_reboot(self):
        self.m.write("/var/lib/ka9q-tune/isolation-reboot.marker", "{{{garbage")
        result = isolation.one_shot(self.env())
        self.assertEqual(result.code, isolation.EXIT_STAGED_NOT_ACTIVE)
        self.assertEqual(result.status, "reboot-spent")

    def test_marker_write_failure_prevents_the_reboot(self):
        # Rebooting without a marker turns one reboot into a loop.
        env = self.m.env(REBOOT=self.recorder.command(), GRUB_UPDATE="true",
                         ISOL_MARKER="/proc/definitely/not/writable/marker")
        result = isolation.one_shot(env)
        self.assertEqual(result.code, isolation.EXIT_STAGED_NOT_ACTIVE)
        self.assertEqual(result.status, "marker-write-failed")
        self.assertIsNone(self.recorder.saw_marker())

    def test_exit_codes_are_the_documented_contract(self):
        self.assertEqual(isolation.EXIT_NOTHING_TO_DO, 0)
        self.assertEqual(isolation.EXIT_REBOOT_INVOKED, 10)
        self.assertEqual(isolation.EXIT_STAGED_NOT_ACTIVE, 20)


class SpellingTest(unittest.TestCase):
    def test_range_vs_list_does_not_cause_a_reboot(self):
        # The reboot-forever bug, end to end: drop-in says 12-13, the running
        # kernel delivered 12,13, and nothing should happen.
        m = fakeroot.healthy()
        try:
            m.topology(logical=16)
            m.dropin("12-13", mtime=fakeroot.BOOT_TIME - 60)
            m.nohz_full("12,13").isolated("12,13").rcu_offload([12, 13])
            result = isolation.one_shot(m.env(REBOOT="false", GRUB_UPDATE="false"))
            self.assertEqual(result.code, isolation.EXIT_NOTHING_TO_DO)
        finally:
            m.destroy()


if __name__ == "__main__":
    unittest.main()
