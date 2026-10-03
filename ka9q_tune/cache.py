"""R5: partition L3 by bytes, never by mask.

The same ten-way mask `L3:0=3ff` is 10 MiB on a 16 MiB part and 5 MiB on an
8 MiB one. So the target is expressed in bytes and the mask is computed from
live geometry, and the kernel's own <group>/size is what the result is checked
against.

Measured sweep on an 8 MiB part, 16 ways, radiod's fft thread:

    3 MiB  99.8%     6 MiB  87.1%
    4 MiB  90.5%     7 MiB  84.7%
    5 MiB  84.4%     8 MiB  84.6%

There is a cliff below ~4 MiB and a knee at ~5 MiB, past which more cache buys
nothing measurable. Worth setting; not worth over-tuning.
"""

import os
import re

from . import topology as topo

DEFAULT_TARGET_BYTES = 5 * 1024 * 1024   # the knee
CLIFF_BYTES = 4 * 1024 * 1024            # below this, the fft thread falls off
DEFAULT_GROUP = "radiod"


class Unavailable(Exception):
    pass


class Resctrl:
    def __init__(self, env, topology):
        self.env = env
        self.topology = topology
        self.root = env.path("RESCTRL")
        self.info = os.path.join(self.root, "info", "L3")

    # -- geometry ---------------------------------------------------------

    @property
    def mounted(self):
        return self.env.exists(os.path.join(self.root, "schemata"))

    @property
    def available(self):
        return self.mounted and self.env.exists(os.path.join(self.info, "cbm_mask"))

    def cbm_mask(self):
        raw = self.env.read_stripped(os.path.join(self.info, "cbm_mask"), "")
        return int(raw, 16) if raw else 0

    def num_bits(self):
        """Ways in the cache, i.e. the width of the capacity bitmask."""
        return bin(self.cbm_mask()).count("1")

    def min_bits(self):
        value = self.env.read_int(os.path.join(self.info, "min_cbm_bits"))
        return value if value and value > 0 else 1

    def domains(self):
        """L3 cache instance ids present in the default schemata."""
        found = parse_schemata(self.env.read(os.path.join(self.root, "schemata"), ""), 16)
        return sorted(found.get("L3", {}))

    def domain_bytes(self, domain):
        """Total bytes of one L3 instance, from the CPUs that share it."""
        for cpu in self.topology.online:
            if self.topology.l3_domain(cpu) == domain:
                size = self.topology.l3_bytes(cpu)
                if size:
                    return size
        for cpu in self.topology.online:
            size = self.topology.l3_bytes(cpu)
            if size:
                return size
        return None

    def bytes_per_way(self, domain):
        total = self.domain_bytes(domain)
        bits = self.num_bits()
        if not total or not bits:
            return None
        return total / bits

    # -- the computation R5 is about --------------------------------------

    def ways_for_bytes(self, domain, target_bytes):
        """Ways needed to reach target_bytes on THIS part. Returns (ways, note)."""
        per_way = self.bytes_per_way(domain)
        bits = self.num_bits()
        if not per_way or not bits:
            raise Unavailable("L3 geometry unknown for domain %s" % domain)
        ways = int(-(-target_bytes // int(per_way)))  # ceil
        note = None
        if ways < self.min_bits():
            ways = self.min_bits()
            note = "raised to the kernel's min_cbm_bits (%d)" % ways
        if ways > bits:
            ways = bits
            note = ("the whole cache is smaller than the %s target; allocating "
                    "all %d ways" % (topo.human_bytes(target_bytes), bits))
        return ways, note

    @staticmethod
    def mask_for_ways(ways, bits):
        """A contiguous low-bit mask. Contiguity is a hardware requirement."""
        ways = max(0, min(ways, bits))
        return (1 << ways) - 1

    @staticmethod
    def complement(mask, bits):
        return ((1 << bits) - 1) & ~mask

    # -- groups -----------------------------------------------------------

    def group_path(self, group):
        return os.path.join(self.root, group)

    def group_size(self, group):
        """Bytes the kernel says the group has. Authoritative; use it."""
        path = os.path.join(self.group_path(group), "size")
        parsed = parse_schemata(self.env.read(path, ""), 10)
        return parsed.get("L3", {})

    def group_schemata(self, group):
        path = os.path.join(self.group_path(group), "schemata")
        return parse_schemata(self.env.read(path, ""), 16)

    def apply(self, group, target_bytes, tids, exclusive=True):
        """Give `group` at least target_bytes of each L3, and move tids into it.

        When exclusive, the default group is reduced to the complementary mask
        so the allocation is a partition rather than an overlap -- a group that
        shares its ways with everything else on the machine is not a partition
        and will not behave like one.

        Returns (ok, [messages]).
        """
        messages = []
        if not self.available:
            return False, [
                "resctrl is not available at %s: L3 partitioning cannot be "
                "applied or verified on this machine" % self.root
            ]
        if target_bytes < CLIFF_BYTES:
            messages.append(
                "warning: %s is below the ~%s cliff measured on an 8 MiB part; "
                "the fft thread is expected to degrade sharply"
                % (topo.human_bytes(target_bytes), topo.human_bytes(CLIFF_BYTES))
            )

        bits = self.num_bits()
        group_masks, root_masks = {}, {}
        for domain in self.domains():
            try:
                ways, note = self.ways_for_bytes(domain, target_bytes)
            except Unavailable as exc:
                return False, messages + [str(exc)]
            if note:
                messages.append("L3:%s %s" % (domain, note))
            mask = self.mask_for_ways(ways, bits)
            group_masks[domain] = mask
            rest = self.complement(mask, bits)
            if rest == 0:
                messages.append(
                    "L3:%s leaves no ways for the rest of the system; not "
                    "constraining the default group" % domain
                )
                rest = (1 << bits) - 1
            root_masks[domain] = rest

        path = self.group_path(group)
        if not self.env.exists(path):
            if self.env.dry_run:
                messages.append("dry-run: would create %s" % path)
            else:
                try:
                    os.mkdir(path)
                    messages.append("created %s" % path)
                except OSError as exc:
                    return False, messages + ["could not create %s: %s" % (path, exc)]

        if exclusive:
            ok, detail = self.env.write(
                os.path.join(self.root, "schemata"), format_schemata(root_masks) + "\n"
            )
            messages.append(detail if ok else "default group: " + detail)
            if not ok:
                return False, messages

        ok, detail = self.env.write(
            os.path.join(path, "schemata"), format_schemata(group_masks) + "\n"
        )
        messages.append(detail if ok else "%s: %s" % (group, detail))
        if not ok:
            return False, messages

        moved, failed = 0, 0
        for tid in tids:
            ok, _ = self.env.write(os.path.join(path, "tasks"), "%d\n" % tid)
            moved += 1 if ok else 0
            failed += 0 if ok else 1
        messages.append("moved %d threads into %s%s"
                        % (moved, group, (", %d failed" % failed) if failed else ""))

        # Never report success from configuration: read the size back.
        sizes = self.group_size(group)
        if not sizes and not self.env.dry_run:
            messages.append("could not read %s/size; allocation is UNVERIFIED"
                            % self.group_path(group))
            return False, messages
        for domain, size in sorted(sizes.items()):
            total = self.domain_bytes(domain)
            messages.append(
                "verified L3:%s = %s%s"
                % (domain, topo.human_bytes(size),
                   (" of %s" % topo.human_bytes(total)) if total else "")
            )
            if size < target_bytes:
                messages.append(
                    "L3:%s delivered %s, less than the %s asked for"
                    % (domain, topo.human_bytes(size), topo.human_bytes(target_bytes))
                )
                return False, messages
        return True, messages


_SCHEMA_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*:\s*(.*)$")


def parse_schemata(text, base=16):
    """Parse a resctrl schemata or size file into {resource: {domain: value}}.

    The two files share a layout but not a radix: schemata carries hex capacity
    bitmasks (`L3:0=3ff;1=3ff`) and size carries decimal bytes
    (`L3:0=5242880`). The radix is passed in rather than guessed, because a
    guess based on the shape of the digits silently misreads values like
    `2048` and there is no way to notice from the result.
    """
    out = {}
    for line in (text or "").splitlines():
        m = _SCHEMA_LINE.match(line)
        if not m:
            continue
        resource, body = m.group(1), m.group(2)
        entries = {}
        for item in body.split(";"):
            if "=" not in item:
                continue
            domain, _, value = item.partition("=")
            try:
                entries[int(domain.strip())] = int(value.strip(), base)
            except ValueError:
                continue
        if entries:
            out[resource] = entries
    return out


def format_schemata(masks, resource="L3"):
    body = ";".join("%d=%x" % (d, m) for d, m in sorted(masks.items()))
    return "%s:%s" % (resource, body)
