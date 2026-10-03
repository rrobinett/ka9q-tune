"""R5: the same mask is a different amount of cache on a different part."""

import unittest

import fakeroot
from ka9q_tune import cache, topology as topo_mod


class GeometryTest(unittest.TestCase):
    def build(self, l3_bytes, cbm="ffff"):
        m = fakeroot.Machine()
        m.topology(l3_bytes=l3_bytes).resctrl(cbm_mask=cbm)
        return m

    def test_same_mask_means_different_bytes(self):
        # L3:0=3ff is ten ways either way. On a 16 MiB part that is 10 MiB; on
        # an 8 MiB part it is 5 MiB. A tool that configures masks cannot know
        # which it just did.
        small = self.build(8 * 1024 * 1024)
        large = self.build(16 * 1024 * 1024)
        try:
            r_small = cache.Resctrl(small.env(), topo_mod.Topology(small.env()))
            r_large = cache.Resctrl(large.env(), topo_mod.Topology(large.env()))
            ten_ways = 10
            self.assertEqual(int(r_small.bytes_per_way(0)) * ten_ways,
                             5 * 1024 * 1024)
            self.assertEqual(int(r_large.bytes_per_way(0)) * ten_ways,
                             10 * 1024 * 1024)
        finally:
            small.destroy()
            large.destroy()

    def test_same_byte_target_means_different_masks(self):
        small = self.build(8 * 1024 * 1024)
        large = self.build(16 * 1024 * 1024)
        try:
            r_small = cache.Resctrl(small.env(), topo_mod.Topology(small.env()))
            r_large = cache.Resctrl(large.env(), topo_mod.Topology(large.env()))
            target = 5 * 1024 * 1024
            ways_small, _ = r_small.ways_for_bytes(0, target)
            ways_large, _ = r_large.ways_for_bytes(0, target)
            self.assertEqual(ways_small, 10)
            self.assertEqual(ways_large, 5)
            self.assertNotEqual(r_small.mask_for_ways(ways_small, 16),
                                r_large.mask_for_ways(ways_large, 16))
        finally:
            small.destroy()
            large.destroy()

    def test_mask_is_contiguous(self):
        m = self.build(8 * 1024 * 1024)
        try:
            r = cache.Resctrl(m.env(), topo_mod.Topology(m.env()))
            for ways in range(1, 17):
                mask = r.mask_for_ways(ways, 16)
                binary = bin(mask)[2:]
                self.assertNotIn("01", binary, "mask %x is not contiguous" % mask)
                self.assertEqual(bin(mask).count("1"), ways)
        finally:
            m.destroy()

    def test_complement_covers_the_rest(self):
        self.assertEqual(cache.Resctrl.complement(0x3ff, 16), 0xfc00)
        self.assertEqual(cache.Resctrl.complement(0x3ff, 16) | 0x3ff, 0xffff)

    def test_min_cbm_bits_is_respected(self):
        m = fakeroot.Machine()
        m.topology(l3_bytes=8 * 1024 * 1024).resctrl(cbm_mask="ffff", min_bits=4)
        try:
            r = cache.Resctrl(m.env(), topo_mod.Topology(m.env()))
            ways, note = r.ways_for_bytes(0, 512 * 1024)
            self.assertEqual(ways, 4)
            self.assertIn("min_cbm_bits", note)
        finally:
            m.destroy()

    def test_target_larger_than_the_cache_is_clamped_and_said_so(self):
        m = self.build(8 * 1024 * 1024)
        try:
            r = cache.Resctrl(m.env(), topo_mod.Topology(m.env()))
            ways, note = r.ways_for_bytes(0, 32 * 1024 * 1024)
            self.assertEqual(ways, 16)
            self.assertIn("whole cache", note)
        finally:
            m.destroy()


class ParsingTest(unittest.TestCase):
    def test_schemata_is_hex_and_size_is_decimal(self):
        # Guessing the radix from the digits misreads values like 2048, and
        # nothing in the result reveals it.
        schemata = cache.parse_schemata("    L3:0=3ff;1=3ff\n", 16)
        self.assertEqual(schemata["L3"][0], 0x3ff)
        size = cache.parse_schemata("    L3:0=5242880;1=5242880\n", 10)
        self.assertEqual(size["L3"][0], 5242880)
        ambiguous = cache.parse_schemata("    L3:0=2048\n", 10)
        self.assertEqual(ambiguous["L3"][0], 2048)

    def test_format_round_trips(self):
        text = cache.format_schemata({0: 0x3ff, 1: 0xfc00})
        self.assertEqual(cache.parse_schemata(text, 16)["L3"],
                         {0: 0x3ff, 1: 0xfc00})


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.m = fakeroot.healthy()

    def tearDown(self):
        self.m.destroy()

    def test_apply_verifies_against_the_kernels_own_size_file(self):
        env = self.m.env()
        r = cache.Resctrl(env, topo_mod.Topology(env))
        ok, messages = r.apply("radiod", 5 * 1024 * 1024, [fakeroot.FFT_TID])
        self.assertTrue(ok, messages)
        self.assertTrue(any("verified" in m for m in messages), messages)

    def test_apply_fails_loudly_when_the_kernel_delivered_less(self):
        # The size file is authoritative. If it disagrees with what was asked
        # for, that is a failure, not a rounding detail to swallow.
        env = self.m.env()
        self.m.write("/sys/fs/resctrl/radiod/size", "L3:0=1048576\n")
        r = cache.Resctrl(env, topo_mod.Topology(env))
        ok, messages = r.apply("radiod", 5 * 1024 * 1024, [fakeroot.FFT_TID])
        self.assertFalse(ok)
        self.assertTrue(any("less than" in m for m in messages), messages)

    def test_apply_shrinks_the_default_group_so_it_is_a_partition(self):
        env = self.m.env()
        r = cache.Resctrl(env, topo_mod.Topology(env))
        r.apply("radiod", 5 * 1024 * 1024, [fakeroot.FFT_TID])
        root_mask = cache.parse_schemata(
            env.read(self.m.path("/sys/fs/resctrl/schemata")), 16)["L3"][0]
        group_mask = cache.parse_schemata(
            env.read(self.m.path("/sys/fs/resctrl/radiod/schemata")), 16)["L3"][0]
        self.assertEqual(root_mask & group_mask, 0,
                         "an overlapping allocation is not a partition")

    def test_below_the_cliff_warns(self):
        env = self.m.env()
        r = cache.Resctrl(env, topo_mod.Topology(env))
        _, messages = r.apply("radiod", 3 * 1024 * 1024, [fakeroot.FFT_TID])
        self.assertTrue(any("cliff" in m for m in messages), messages)

    def test_missing_resctrl_is_reported_not_skipped(self):
        m = fakeroot.Machine()
        m.topology()
        try:
            env = m.env()
            r = cache.Resctrl(env, topo_mod.Topology(env))
            ok, messages = r.apply("radiod", 5 * 1024 * 1024, [1])
            self.assertFalse(ok)
            self.assertTrue(any("not available" in m for m in messages))
        finally:
            m.destroy()


class HumanBytesTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(topo_mod.human_bytes(5 * 1024 * 1024), "5.00 MiB")
        self.assertEqual(topo_mod.parse_size("8192K"), 8 * 1024 * 1024)
        self.assertEqual(topo_mod.parse_size("16M"), 16 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
