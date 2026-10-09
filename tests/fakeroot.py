"""A fake machine on disk, so none of this needs real hardware to test.

Defaults model the measured 6-core / 12-thread Ryzen 5 5560U station: 12
logical CPUs in sequential sibling pairs, an 8 MiB 16-way L3, radiod on 8-9,
CONFIG_HZ=250.
"""

import os
import shutil
import tempfile

from ka9q_tune.env import Env

RADIOD_PID = 193916
FFT_TID = RADIOD_PID + 7
INGEST_TID = RADIOD_PID + 8

FFT_GEN = "/usr/local/bin/fft-gen"

BOOT_TIME = 1_759_000_000          # epoch seconds the fake kernel booted
CONFIG_HZ = 250


class Machine:
    def __init__(self, root=None):
        self.root = root or tempfile.mkdtemp(prefix="ka9q-tune-test-")
        self._interrupt_counts = {}
        self._tick_counts = {}
        self._thread_ticks = {}

    # -- plumbing ---------------------------------------------------------

    def destroy(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, relative):
        return os.path.join(self.root, relative.lstrip("/"))

    def write(self, relative, text, mtime=None):
        full = self.path(relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)
        if mtime is not None:
            os.utime(full, (mtime, mtime))
        return full

    def mkdir(self, relative):
        full = self.path(relative)
        os.makedirs(full, exist_ok=True)
        return full

    def remove(self, relative):
        try:
            os.unlink(self.path(relative))
        except OSError:
            pass

    def env(self, sleep=None, **overrides):
        environ = {"KA9Q_TUNE_ROOT": self.root,
                   "KA9Q_TUNE_CLK_TCK": "100",
                   "KA9Q_TUNE_CONFIG_HZ": str(CONFIG_HZ),
                   "KA9Q_TUNE_NOW": str(BOOT_TIME + 3600),
                   # Inside the fixture, so the planner a test sees never
                   # depends on whether the machine running it has fft-gen.
                   "KA9Q_TUNE_FFT_GEN": self.path(FFT_GEN)}
        for key, value in overrides.items():
            environ["KA9Q_TUNE_" + key] = str(value)
        return Env(environ, sleep=sleep or (lambda _seconds: None))

    # -- building blocks --------------------------------------------------

    def topology(self, logical=12, siblings="sequential", l3_bytes=8 * 1024 * 1024):
        self.write("/sys/devices/system/cpu/online", "0-%d\n" % (logical - 1))
        half = logical // 2
        for cpu in range(logical):
            if siblings == "sequential":
                core = cpu // 2
                pair = (core * 2, core * 2 + 1)
            else:                       # split: {0,6},{1,7}, ...
                core = cpu % half
                pair = (core, core + half)
            base = "/sys/devices/system/cpu/cpu%d/topology" % cpu
            self.write(base + "/thread_siblings_list", "%d-%d\n" % pair
                       if pair[1] == pair[0] + 1 else "%d,%d\n" % pair)
            self.write(base + "/core_id", "%d\n" % core)
            self.write(base + "/physical_package_id", "0\n")
            idx = "/sys/devices/system/cpu/cpu%d/cache/index3" % cpu
            self.write(idx + "/level", "3\n")
            self.write(idx + "/type", "Unified\n")
            self.write(idx + "/size", "%dK\n" % (l3_bytes // 1024))
            self.write(idx + "/id", "0\n")
        return self

    def cmdline(self, text):
        self.write("/proc/cmdline", text + "\n")
        return self

    def boot(self, btime=BOOT_TIME):
        self.write("/proc/stat", "cpu  1 2 3 4 5 6 7 8\nbtime %d\nprocesses 99\n"
                   % btime)
        return self

    def nohz_full(self, value):
        """Delivered nohz_full. Pass None to model a kernel without the file."""
        if value is None:
            self.remove("/sys/devices/system/cpu/nohz_full")
        else:
            self.write("/sys/devices/system/cpu/nohz_full", value + "\n")
        return self

    def isolated(self, value):
        self.write("/sys/devices/system/cpu/isolated", value + "\n")
        return self

    def rcu_offload(self, cpus):
        """Create the rcuop/N kthreads that prove rcu_nocbs was delivered."""
        for n, cpu in enumerate(sorted(cpus)):
            pid = 400 + n
            self.write("/proc/%d/comm" % pid, "rcuop/%d\n" % cpu)
            self.write("/proc/%d/cmdline" % pid, "")
        return self

    def dropin(self, cpus, mtime=None, text=None):
        body = text if text is not None else (
            'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT '
            'isolcpus=%s nohz_full=%s rcu_nocbs=%s"\n' % (cpus, cpus, cpus)
        )
        self.write("/etc/default/grub.d/99-ka9q-isolation.cfg", body, mtime=mtime)
        return self

    def grub_cfg(self, text=None, cpus="8-9"):
        body = text if text is not None else (
            "menuentry 'Debian' {\n"
            "  linux /vmlinuz root=/dev/sda1 ro quiet isolcpus=%s nohz_full=%s "
            "rcu_nocbs=%s\n}\n" % (cpus, cpus, cpus)
        )
        self.write("/boot/grub/grub.cfg", body)
        return self

    # -- processes --------------------------------------------------------

    def radiod(self, pid=RADIOD_PID, cpus="8-9", unit="radiod@WB6CXC-7",
               threads=None, started=BOOT_TIME + 60):
        threads = threads or {FFT_TID: "fft", INGEST_TID: "proc_rx888",
                              pid: "radiod"}
        self.write("/proc/%d/comm" % pid, "radiod\n")
        self.write("/proc/%d/cmdline" % pid,
                   "radiod\0/etc/radio/%s.conf\0" % unit.split("@")[-1])
        self.write("/proc/%d/cgroup" % pid,
                   "0::/system.slice/system-radiod.slice/%s.service\n" % unit)
        # Field 22, starttime, in clock ticks since boot (CLK_TCK 100).
        fields = ["0"] * 50
        fields[19] = str(int((started - BOOT_TIME) * 100))
        self.write("/proc/%d/stat" % pid,
                   "%d (radiod) %s\n" % (pid, " ".join(["S"] + fields[1:])))
        self.write("/proc/%d/status" % pid, "Name:\tradiod\nCpus_allowed_list:\t%s\n" % cpus)
        for tid, name in threads.items():
            base = "/proc/%d/task/%d" % (pid, tid)
            self.write(base + "/comm", name + "\n")
            self.write(base + "/status",
                       "Name:\t%s\nCpus_allowed_list:\t%s\n" % (name, cpus))
            self._thread_ticks[tid] = 0
            self.thread_stat(pid, tid, name, 0, 0)
        self.radiod_pid = pid
        return self

    def thread_stat(self, pid, tid, name, utime, stime, on_cpu=8):
        # Fields: pid (comm) state ppid ... utime(14) stime(15) ... processor(39)
        fields = ["0"] * 50
        fields[0] = "S"                      # field 3
        fields[11] = str(utime)              # field 14
        fields[12] = str(stime)              # field 15
        fields[36] = str(on_cpu)             # field 39
        self.write("/proc/%d/task/%d/stat" % (pid, tid),
                   "%d (%s) %s\n" % (tid, name, " ".join(fields)))
        return self

    def advance_threads(self, pid, per_thread_ticks):
        """Add CPU time to threads, as a second sample would see."""
        for tid, ticks in per_thread_ticks.items():
            self._thread_ticks[tid] = self._thread_ticks.get(tid, 0) + ticks
            with open(self.path("/proc/%d/task/%d/comm" % (pid, tid))) as fh:
                name = fh.read().strip()
            self.thread_stat(pid, tid, name, self._thread_ticks[tid], 0)
        return self

    # -- interrupts -------------------------------------------------------

    def interrupts(self, cpus=12, loc=None, device=None):
        """Write /proc/interrupts. loc and device are {cpu: count}."""
        self._tick_counts = dict(loc or {})
        self._interrupt_counts = dict(device or {})
        self._render_interrupts(cpus)
        return self

    def advance_interrupts(self, seconds, loc_rate=None, device_rate=None,
                           cpus=12):
        """Add counts as `seconds` of the given per-CPU rates would."""
        for cpu, rate in (loc_rate or {}).items():
            self._tick_counts[cpu] = self._tick_counts.get(cpu, 0) + int(rate * seconds)
        for key, per_cpu in (device_rate or {}).items():
            bucket = self._interrupt_counts.setdefault(key, {})
            for cpu, rate in per_cpu.items():
                bucket[cpu] = bucket.get(cpu, 0) + int(rate * seconds)
        self._render_interrupts(cpus)
        return self

    def _render_interrupts(self, cpus):
        header = "      " + "".join("%11s" % ("CPU%d" % c) for c in range(cpus))
        lines = [header]
        for key, per_cpu in sorted(self._interrupt_counts.items()):
            label = "IR-PCI-MSI  xhci_hcd" if key.isdigit() else ""
            lines.append("%4s:%s   %s" % (
                key, "".join("%11d" % per_cpu.get(c, 0) for c in range(cpus)), label))
        if self._tick_counts:
            lines.append(" LOC:%s   Local timer interrupts" % "".join(
                "%11d" % self._tick_counts.get(c, 0) for c in range(cpus)))
        self.write("/proc/interrupts", "\n".join(lines) + "\n")

    def irq(self, number, affinity="0-1", name="xhci_hcd"):
        base = "/proc/irq/%s" % number
        self.write(base + "/smp_affinity_list", affinity + "\n")
        self.write(base + "/effective_affinity_list", affinity + "\n")
        self.mkdir(base + "/" + name)
        return self

    # -- resctrl ----------------------------------------------------------

    def resctrl(self, cbm_mask="ffff", min_bits=1, domains=(0,), group=None,
                group_mask=None):
        self.write("/sys/fs/resctrl/info/L3/cbm_mask", cbm_mask + "\n")
        self.write("/sys/fs/resctrl/info/L3/min_cbm_bits", "%d\n" % min_bits)
        self.write("/sys/fs/resctrl/schemata",
                   "L3:" + ";".join("%d=%s" % (d, cbm_mask) for d in domains) + "\n")
        if group:
            mask = group_mask or cbm_mask
            self.write("/sys/fs/resctrl/%s/schemata" % group,
                       "L3:" + ";".join("%d=%s" % (d, mask) for d in domains) + "\n")
            bits = bin(int(mask, 16)).count("1")
            total = 8 * 1024 * 1024
            per_bit = total // bin(int(cbm_mask, 16)).count("1")
            self.write("/sys/fs/resctrl/%s/size" % group,
                       "L3:" + ";".join("%d=%d" % (d, bits * per_bit)
                                        for d in domains) + "\n")
            self.write("/sys/fs/resctrl/%s/tasks" % group, "")
        return self

    # -- cpufreq ----------------------------------------------------------

    def cpufreq(self, cpus, min_khz=3_200_000, max_khz=3_200_000,
                cur_khz=3_200_000, hw_min=400_000, hw_max=3_200_000,
                driver="amd-pstate-epp", hardware_readback=True):
        for cpu in cpus:
            base = "/sys/devices/system/cpu/cpu%d/cpufreq" % cpu
            self.write(base + "/scaling_min_freq", "%d\n" % min_khz)
            self.write(base + "/scaling_max_freq", "%d\n" % max_khz)
            self.write(base + "/scaling_cur_freq", "%d\n" % cur_khz)
            self.write(base + "/cpuinfo_min_freq", "%d\n" % hw_min)
            self.write(base + "/cpuinfo_max_freq", "%d\n" % hw_max)
            self.write(base + "/scaling_driver", driver + "\n")
            self.write(base + "/scaling_governor", "performance\n")
            if hardware_readback:
                self.write(base + "/cpuinfo_cur_freq", "%d\n" % cur_khz)
            else:
                self.remove(base + "/cpuinfo_cur_freq")
        return self

    def fft_gen(self):
        """Install radiod's planner, as a ka9q-radio built since 2026 has."""
        full = self.write(FFT_GEN, "#!/bin/sh\nexit 0\n")
        os.chmod(full, 0o755)
        return self

    def fft_log(self, text=None, mtime=None):
        self.write("/var/lib/ka9q-radio/fft.log", text if text is not None else "",
                   mtime=mtime)
        return self


# -- ready-made stations --------------------------------------------------

def healthy(root=None):
    """WB6CXC-7 after the fix: isolated, tickless, partitioned, wisdom complete."""
    m = Machine(root)
    m.topology().boot().cmdline(
        "BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro quiet isolcpus=8-9 "
        "nohz_full=8-9 rcu_nocbs=8-9"
    )
    m.nohz_full("8-9").isolated("8-9").rcu_offload([8, 9])
    m.dropin("8-9", mtime=BOOT_TIME - 3600).grub_cfg()
    m.radiod()
    m.interrupts(loc={c: 1000 for c in range(12)},
                 device={"130": {0: 5000, 1: 5000}})
    m.irq("130", affinity="0-1")
    m.resctrl(group="radiod", group_mask="3ff")
    m.cpufreq(range(12))
    m.fft_log("")
    return m


def broken(root=None):
    """WB6CXC-7 before the fix: nothing isolated, the full tick, everything green."""
    m = Machine(root)
    m.topology().boot().cmdline("BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro quiet")
    m.nohz_full("").isolated("")
    m.dropin("8-9", mtime=BOOT_TIME + 41 * 60)     # written 41 min AFTER boot
    m.grub_cfg()
    m.radiod()
    m.interrupts(loc={c: 1000 for c in range(12)},
                 device={"130": {0: 5000, 1: 5000}})
    m.irq("130", affinity="0-1")
    m.cpufreq(range(12))
    m.fft_log("")
    return m
