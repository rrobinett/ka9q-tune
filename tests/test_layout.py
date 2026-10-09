"""Issue #2: the pair layout is not always faster, and status must judge
placement by the two hot threads, not by every thread radiod has.

Measured on dp0 (Skylake-SP Xeon, 2026-10-08): fft 74% with proc_rx888 on its
hyperthread sibling, 62% with proc_rx888 on another core. The fixtures below
replay that as thread rates, so the A/B/A measurement runs end to end.
"""

import io
import unittest
from unittest import mock

import fakeroot
from fakeroot import FFT_TID, INGEST_TID, RADIOD_PID
from ka9q_tune import cli, layout as layout_mod, radiod as radiod_mod, report
from ka9q_tune import topology as topo_mod


def run(machine, argv, sleep=None, **env_overrides):
    out = io.StringIO()
    environ = {"KA9Q_TUNE_ROOT": machine.root, "KA9Q_TUNE_CLK_TCK": "100",
               "KA9Q_TUNE_CONFIG_HZ": str(fakeroot.CONFIG_HZ)}
    for key, value in env_overrides.items():
        environ["KA9Q_TUNE_" + key] = str(value)
    code = cli.main(argv, out=out, environ=environ,
                    sleep=sleep or (lambda _seconds: None))
    return code, out.getvalue()


def radiod_for(machine, **overrides):
    env = machine.env(**overrides)
    return radiod_mod.Radiod(env, topo_mod.Topology(env))


class PlacementTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_all_threads_on_one_core_is_the_pair_layout(self):
        self.m = fakeroot.healthy()
        self.assertEqual(radiod_for(self.m).placement().layout, radiod_mod.PAIR)

    def test_one_hot_thread_per_core_is_the_split_layout(self):
        self.m = fakeroot.split()
        placement = radiod_for(self.m).placement()
        self.assertEqual(placement.layout, radiod_mod.SPLIT)
        self.assertEqual(placement.idle_siblings(), frozenset({9, 11}))

    def test_sharing_two_cores_unpinned_is_neither(self):
        # dp0's host before the move: both hot threads allowed on 2 and 4.
        self.m = fakeroot.healthy()
        self.m.thread_affinity(FFT_TID, "8,10").thread_affinity(INGEST_TID, "8,10")
        self.assertIsNone(radiod_for(self.m).placement().layout)

    def test_hot_cpus_ignore_the_minor_threads(self):
        self.m = fakeroot.split()
        r = radiod_for(self.m)
        self.assertEqual(r.hot_cpus(), frozenset({8, 10}))
        self.assertIn(0, r.process_affinity())

    def test_choose_split_takes_whole_cores_and_never_the_boot_core(self):
        self.m = fakeroot.split()
        env = self.m.env()
        topology = topo_mod.Topology(env)
        fft, ingest, isolate, reason = radiod_mod.choose_split(
            topology, frozenset({8, 9, 10, 11}))
        self.assertEqual((fft, ingest), (8, 10))
        self.assertEqual(isolate, frozenset({8, 9, 10, 11}))
        self.assertNotIn(0, isolate)
        self.assertNotIn(1, isolate)
        self.assertIn("fully isolated", reason)

    def test_choose_split_skips_the_boot_core_even_when_nothing_is_isolated(self):
        # With no isolation, CPU number decides, and core 0 would win.
        self.m = fakeroot.healthy()
        topology = topo_mod.Topology(self.m.env())
        fft, ingest, isolate, _ = radiod_mod.choose_split(topology, frozenset())
        self.assertFalse(isolate & {0, 1}, isolate)
        self.assertEqual((fft, ingest), (2, 4))

    def test_pin_split_puts_each_thread_where_it_belongs(self):
        self.m = fakeroot.healthy()
        r = radiod_for(self.m)
        moves = {}
        with mock.patch.object(r, "set_affinity",
                               lambda tid, cpus: moves.__setitem__(tid, frozenset(cpus))):
            ok, messages = r.pin_split(8, 10, frozenset(range(8)))
        self.assertTrue(ok, messages)
        self.assertEqual(moves[FFT_TID], frozenset({8}))
        self.assertEqual(moves[INGEST_TID], frozenset({10}))
        self.assertEqual(moves[RADIOD_PID], frozenset(range(8)))


class StatusTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def status(self):
        return report.collect(self.m.env(), seconds=0.0, sleep=lambda _s: None)

    def line(self, status, label):
        return [l for l in status.lines if l.label == label][0]

    def test_a_correct_split_station_is_healthy(self):
        # Before the fix: "radiod is on the boot CPU", isolation PARTIAL, the
        # tick on every housekeeping CPU, all BAD -- from the minor threads.
        self.m = fakeroot.split()
        status = self.status()
        radiod_line = self.line(status, "radiod")
        self.assertEqual(radiod_line.state, report.OK, radiod_line.notes)
        self.assertIn("split", radiod_line.value)
        self.assertEqual(self.line(status, "isolation").state, report.OK)
        self.assertNotIn("boot CPU", " ".join(radiod_line.notes))

    def test_split_with_a_sibling_left_unisolated_warns(self):
        self.m = fakeroot.split()
        self.m.isolate("8-10", [8, 9, 10])
        radiod_line = self.line(self.status(), "radiod")
        self.assertEqual(radiod_line.state, report.WARN)
        self.assertIn("11", " ".join(radiod_line.notes))

    def test_hot_threads_sharing_two_cores_warn_and_name_both_layouts(self):
        self.m = fakeroot.healthy()
        self.m.thread_affinity(FFT_TID, "8,10").thread_affinity(INGEST_TID, "8,10")
        radiod_line = self.line(self.status(), "radiod")
        self.assertEqual(radiod_line.state, report.WARN)
        text = " ".join(radiod_line.notes)
        self.assertIn("--layout pair", text)
        self.assertIn("--layout split", text)

    def test_the_pair_layout_still_reads_as_before(self):
        self.m = fakeroot.healthy()
        radiod_line = self.line(self.status(), "radiod")
        self.assertEqual(radiod_line.state, report.OK)
        self.assertIn("core 4, sibling pair", radiod_line.value)


class SplitCommandTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_stage_split_isolates_both_cores_whole(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "stage", "--layout", "split",
                                  "--cpus", "8,10"], GRUB_UPDATE="true")
        self.assertIn("isolcpus=8-11", text)
        self.assertIn("nohz_full=8-11", text)

    def test_split_on_one_core_is_refused(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "pin", "--layout", "split",
                                  "--cpus", "8,9"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("share a physical core", text)

    def test_split_on_the_boot_cpu_is_refused(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "pin", "--layout", "split",
                                  "--cpus", "0,10"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("boot CPU", text)

    def test_pin_split_dry_run_names_every_placement(self):
        self.m = fakeroot.healthy()
        code, text = run(self.m, ["--dry-run", "pin", "--layout", "split",
                                  "--cpus", "8,10"])
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertIn("fft to cpu8", text)
        self.assertIn("proc_rx888 to cpu10", text)

    def test_irq_on_a_housekeeping_cpu_is_not_radiods_problem(self):
        # The minor threads run on 0-7; an interrupt on cpu 3 is where it
        # should be. Judging by every thread's affinity flagged it.
        self.m = fakeroot.split()
        self.m.interrupts(loc={c: 1000 for c in range(12)},
                          device={"130": {3: 5000}})

        def advance(seconds):
            if seconds > 0:
                self.m.advance_interrupts(seconds, device_rate={"130": {3: 900.0}})

        code, text = run(self.m, ["irq", "--irq-seconds", "2"], sleep=advance)
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertIn("no interrupt above", text)


class MeasureTest(unittest.TestCase):
    """A/B/A on a fixture whose thread rates depend on where proc_rx888 is."""

    def tearDown(self):
        self.m.destroy()

    def machine(self, pair_rates, split_rates):
        self.m = fakeroot.split()
        self.where = {}
        r = radiod_for(self.m)

        def set_affinity(tid, cpus):
            self.where[tid] = frozenset(cpus)
            self.m.thread_affinity(tid, ",".join(str(c) for c in sorted(cpus)))

        def advance(seconds):
            if seconds <= 0:
                return
            ingest = self.where.get(INGEST_TID, frozenset())
            fft_rate, ingest_rate = pair_rates if ingest == {9} else split_rates
            self.m.advance_threads(RADIOD_PID, {
                FFT_TID: round(fft_rate * seconds),
                INGEST_TID: round(ingest_rate * seconds),
            })

        r.set_affinity = set_affinity
        return r, advance

    def test_dp0_measures_split_faster(self):
        r, advance = self.machine(pair_rates=(74.0, 28.0), split_rates=(62.0, 32.0))
        result = layout_mod.measure(r.env, r, 8, 9, 10, seconds=30, settle=0,
                                    sleep=advance)
        self.assertEqual([w.layout for w in result.windows],
                         [radiod_mod.PAIR, radiod_mod.SPLIT, radiod_mod.PAIR])
        self.assertAlmostEqual(result.windows[0].fft, 74.0, delta=0.1)
        self.assertAlmostEqual(result.windows[1].fft, 62.0, delta=0.1)
        self.assertEqual(result.verdict, radiod_mod.SPLIT)

    def test_a_ryzen_like_machine_measures_pair_faster(self):
        r, advance = self.machine(pair_rates=(48.0, 22.0), split_rates=(55.0, 24.0))
        result = layout_mod.measure(r.env, r, 8, 9, 10, seconds=30, settle=0,
                                    sleep=advance)
        self.assertEqual(result.verdict, radiod_mod.PAIR)

    def test_placement_is_restored_after_measuring(self):
        r, advance = self.machine(pair_rates=(74.0, 28.0), split_rates=(62.0, 32.0))
        layout_mod.measure(r.env, r, 8, 9, 10, seconds=1, settle=0, sleep=advance)
        self.assertEqual(self.where[FFT_TID], frozenset({8}))
        self.assertEqual(self.where[INGEST_TID], frozenset({10}))

    def test_placement_is_restored_even_when_measuring_fails(self):
        r, _ = self.machine(pair_rates=(74.0, 28.0), split_rates=(62.0, 32.0))

        def explode(_seconds):
            raise RuntimeError("radiod went away")

        with self.assertRaises(RuntimeError):
            layout_mod.measure(r.env, r, 8, 9, 10, seconds=1, settle=0, sleep=explode)
        self.assertEqual(self.where[INGEST_TID], frozenset({10}))

    def test_candidates_start_from_radiods_current_cpus(self):
        self.m = fakeroot.split()
        env = self.m.env()
        a, a_sibling, b = layout_mod.candidates(
            topo_mod.Topology(env), frozenset({8, 9, 10, 11}),
            current=frozenset({8, 10}))
        self.assertEqual((a, a_sibling), (8, 9))
        self.assertEqual(b, 10)


class VerdictTest(unittest.TestCase):
    def windows(self, pair1, split, pair2):
        class R:
            def __init__(self, fft):
                self.fft, self.ingest = fft, 30.0
        return [layout_mod.Window(radiod_mod.PAIR, 8, 9, R(pair1)),
                layout_mod.Window(radiod_mod.SPLIT, 8, 10, R(split)),
                layout_mod.Window(radiod_mod.PAIR, 8, 9, R(pair2))]

    def test_a_clear_gain_picks_split(self):
        self.assertEqual(layout_mod.verdict(self.windows(74, 62, 74))[0],
                         radiod_mod.SPLIT)

    def test_a_clear_loss_picks_pair(self):
        self.assertEqual(layout_mod.verdict(self.windows(48, 55, 48))[0],
                         radiod_mod.PAIR)

    def test_within_the_margin_picks_neither(self):
        self.assertIsNone(layout_mod.verdict(self.windows(60, 59, 60))[0])

    def test_drift_between_pair_windows_widens_the_margin(self):
        # 6 points apart with 8 points of drift is not a measurement.
        self.assertIsNone(layout_mod.verdict(self.windows(70, 62, 62))[0])


class LayoutCommandTest(unittest.TestCase):
    def tearDown(self):
        self.m.destroy()

    def test_dry_run_names_both_layouts_and_moves_nothing(self):
        self.m = fakeroot.split()
        with mock.patch.object(radiod_mod.Radiod, "set_affinity") as moved:
            code, text = run(self.m, ["--dry-run", "layout"])
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertIn("pair: fft on cpu8, proc_rx888 on cpu9", text)
        self.assertIn("split: fft on cpu8, proc_rx888 on cpu10", text)
        moved.assert_not_called()


if __name__ == "__main__":
    unittest.main()
