"""CPU topology and cache geometry, read from sysfs.

Both sequential (`{0,1},{2,3}`) and split (`{0,8},{1,9}`) hyperthread
enumerations exist in the wild, so sibling pairs are read from
thread_siblings_list and never inferred from the CPU number.
"""

import os

from . import cpuset


class Topology:
    def __init__(self, env):
        self.env = env
        self.online = self._online()
        self.siblings = {}      # cpu -> frozenset of its logical siblings
        self.core_id = {}       # cpu -> physical core id
        self.package_id = {}    # cpu -> physical package id
        self.l3_size = {}       # cpu -> bytes of L3 visible to it
        self.l3_id = {}         # cpu -> L3 cache instance id
        for cpu in self.online:
            self._load_cpu(cpu)

    # -- loading ----------------------------------------------------------

    def _online(self):
        text = self.env.read_stripped(self.env.sys_cpu("online"))
        if text:
            return sorted(cpuset.parse(text))
        found = []
        for name in self.env.listdir(self.env.sys_cpu()):
            if name.startswith("cpu") and name[3:].isdigit():
                found.append(int(name[3:]))
        return sorted(found)

    def _load_cpu(self, cpu):
        base = self.env.sys_cpu("cpu%d" % cpu, "topology")
        sibs = cpuset.parse(self.env.read_stripped(os.path.join(base, "thread_siblings_list"), ""))
        self.siblings[cpu] = sibs if sibs else frozenset([cpu])
        core = self.env.read_int(os.path.join(base, "core_id"))
        if core is not None:
            self.core_id[cpu] = core
        pkg = self.env.read_int(os.path.join(base, "physical_package_id"))
        if pkg is not None:
            self.package_id[cpu] = pkg
        size, cache_id = self._l3(cpu)
        if size:
            self.l3_size[cpu] = size
        if cache_id is not None:
            self.l3_id[cpu] = cache_id

    def _l3(self, cpu):
        cachedir = self.env.sys_cpu("cpu%d" % cpu, "cache")
        for name in self.env.listdir(cachedir):
            if not name.startswith("index"):
                continue
            idx = os.path.join(cachedir, name)
            level = self.env.read_int(os.path.join(idx, "level"))
            ctype = self.env.read_stripped(os.path.join(idx, "type"), "")
            if level != 3 or ctype not in ("Unified", "Data", ""):
                continue
            return (
                parse_size(self.env.read_stripped(os.path.join(idx, "size"), "")),
                self.env.read_int(os.path.join(idx, "id")),
            )
        return None, None

    # -- queries ----------------------------------------------------------

    @property
    def boot_cpu(self):
        """The CPU the kernel booted on, which can never be nohz_full.

        On every Linux platform this package targets that is CPU 0. The
        consequence that matters is checked elsewhere by comparing the
        requested nohz_full against the delivered one, which is a fact rather
        than an assumption.
        """
        return self.online[0] if self.online else 0

    def sibling_pairs(self):
        """Every distinct set of logical CPUs that share one physical core."""
        seen = set()
        pairs = []
        for cpu in self.online:
            sibs = self.siblings.get(cpu, frozenset([cpu]))
            if sibs in seen:
                continue
            seen.add(sibs)
            pairs.append(sibs)
        return pairs

    def is_sibling_pair(self, cpus):
        """True when cpus is exactly the sibling set of one physical core."""
        cpus = frozenset(cpus)
        if not cpus:
            return False
        first = next(iter(cpus))
        return self.siblings.get(first, frozenset([first])) == cpus

    def describe(self, cpus):
        """A short human description of a CPU set's place in the topology."""
        cpus = frozenset(cpus)
        if not cpus:
            return "unpinned"
        if self.is_sibling_pair(cpus):
            core = self.core_id.get(min(cpus))
            if core is not None:
                return "core %d, sibling pair" % core
            return "sibling pair"
        cores = {self.core_id.get(c) for c in cpus}
        cores.discard(None)
        if len(cores) > 1:
            return "%d physical cores, not a sibling pair" % len(cores)
        return "partial core"

    def l3_bytes(self, cpu):
        return self.l3_size.get(cpu)

    def l3_domain(self, cpu):
        """The resctrl cache id (domain) this CPU's L3 belongs to."""
        if cpu in self.l3_id:
            return self.l3_id[cpu]
        return self.package_id.get(cpu, 0)


def parse_size(text):
    """Parse a sysfs cache size such as '8192K' or '16M' into bytes."""
    if not text:
        return None
    text = text.strip()
    mult = 1
    if text and text[-1] in "KkMmGg":
        mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[text[-1].lower()]
        text = text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def human_bytes(value):
    if value is None:
        return "unknown"
    for unit, scale in (("MiB", 1024 ** 2), ("KiB", 1024)):
        if value >= scale:
            return "%.2f %s" % (value / scale, unit)
    return "%d B" % value
