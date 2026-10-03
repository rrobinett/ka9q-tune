"""The discriminator, driven with the figures actually measured on the fleet."""

import unittest

import fakeroot
from ka9q_tune import diagnose

# WB6CXC-7, the same part, the same wisdom, the same two web clients. The two
# rows differ by one kernel command line and a reboot.
BEFORE = diagnose.Reading(94.2, 41.6, 60.0)
AFTER = diagnose.Reading(48.1, 22.0, 60.0)
REF_FFT, REF_INGEST = AFTER.fft, AFTER.ingest


class VerdictTest(unittest.TestCase):
    def verdict(self, reading, ref=(REF_FFT, REF_INGEST)):
        return diagnose.verdict(reading, ref[0], ref[1], "test")

    def test_both_threads_scaling_together_names_the_core(self):
        # 1.81x and 1.82x. proc_rx888 performs no FFT, so nothing about plans
        # or cache can explain it moving with the FFT thread.
        v = self.verdict(BEFORE)
        self.assertEqual(v.code, diagnose.CORE)
        self.assertIn("CORE", v.headline)

    def test_the_fixed_station_is_at_reference(self):
        self.assertEqual(self.verdict(AFTER).code, diagnose.OK)

    def test_fft_alone_elevated_points_at_the_transform(self):
        v = self.verdict(diagnose.Reading(REF_FFT * 1.8, REF_INGEST, 60.0))
        self.assertEqual(v.code, diagnose.PLANS)

    def test_ingest_alone_elevated_points_at_ingest(self):
        v = self.verdict(diagnose.Reading(REF_FFT, REF_INGEST * 1.8, 60.0))
        self.assertEqual(v.code, diagnose.INGEST)

    def test_different_factors_are_not_called_a_core_problem(self):
        v = self.verdict(diagnose.Reading(REF_FFT * 1.9, REF_INGEST * 1.3, 60.0))
        self.assertEqual(v.code, diagnose.MIXED)

    def test_the_ratio_is_not_the_discriminator(self):
        # Every station measured, good and bad, sat between 2.19 and 2.42 --
        # including the broken one. A rule keyed on the ratio would have been
        # silent on the fault this package exists for.
        ratios = [row[1] / row[2] for row in diagnose.FLEET]
        ratios.append(diagnose.FLEET_BAD[1] / diagnose.FLEET_BAD[2])
        self.assertLess(max(ratios) - min(ratios), 0.3)
        self.assertAlmostEqual(BEFORE.ratio, AFTER.ratio, delta=0.15)

    def test_missing_thread_is_unknown_not_healthy(self):
        v = self.verdict(diagnose.Reading(48.1, None, 60.0))
        self.assertEqual(v.code, diagnose.UNKNOWN)
        self.assertIn("proc_rx888", v.headline)

    def test_every_fleet_row_reads_healthy_against_the_fleet_mean(self):
        for name, fft, ingest, _ in diagnose.FLEET:
            v = diagnose.verdict(diagnose.Reading(fft, ingest, 60.0),
                                 diagnose.FLEET_FFT, diagnose.FLEET_INGEST, "fleet")
            self.assertIn(v.code, (diagnose.OK, diagnose.MIXED),
                          "%s read as %s" % (name, v.code))

    def test_the_broken_row_reads_as_core_against_the_fleet_mean(self):
        _, fft, ingest, _ = diagnose.FLEET_BAD
        v = diagnose.verdict(diagnose.Reading(fft, ingest, 60.0),
                             diagnose.FLEET_FFT, diagnose.FLEET_INGEST, "fleet")
        self.assertEqual(v.code, diagnose.CORE)


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.m = fakeroot.healthy()

    def tearDown(self):
        self.m.destroy()

    def test_saved_baseline_is_preferred_over_the_fleet_mean(self):
        env = self.m.env()
        fft, ingest, source = diagnose.reference(env)
        self.assertIn("fleet mean", source)
        diagnose.save_baseline(env, AFTER)
        fft, ingest, source = diagnose.reference(env)
        self.assertEqual((fft, ingest), (AFTER.fft, AFTER.ingest))
        self.assertIn("baseline", source)

    def test_corrupt_baseline_falls_back_rather_than_raising(self):
        env = self.m.env()
        self.m.write("/var/lib/ka9q-tune/baseline.json", "{not json")
        _, _, source = diagnose.reference(env)
        self.assertIn("fleet mean", source)


class ReadingTest(unittest.TestCase):
    def test_reading_is_built_from_two_samples(self):
        from ka9q_tune import procfs
        before = procfs.ThreadSample({1: 0, 2: 0}, {1: "fft", 2: "proc_rx888"})
        after = procfs.ThreadSample({1: 1443, 2: 660}, {1: "fft", 2: "proc_rx888"})
        reading = diagnose.reading_from(before, after, 30.0, 100)
        self.assertAlmostEqual(reading.fft, 48.1, delta=0.1)
        self.assertAlmostEqual(reading.ingest, 22.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
