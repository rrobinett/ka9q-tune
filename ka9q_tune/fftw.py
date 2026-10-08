"""R8: surface FFTW wisdom misses, and converge on them rather than enumerate.

radiod plans with FFTW_WISDOM_ONLY | FFTW_PATIENT and falls back silently to
FFTW_ESTIMATE when wisdom is missing (filter.c:107). An ESTIMATE plan is
heuristic, unmeasured, and kept for the life of the process. The only trace is
a line in fft.log -- so that file IS the list of transforms running on bad
plans. Empty is what you want.

A static list of transform sizes can never be complete: every ka9q-web
spectrum zoom level is a different transform (23 rows in zoom_table[]) and the
channel-filter sizes follow the configured channel set. One station had 25
transforms on ESTIMATE plans with no indication anywhere but that file.

So the planner converges on fft.log instead of enumerating: plan what it
reports, clear it, restart, look again, repeat until a full restart leaves it
empty. That terminates. Enumeration does not.
"""

import os
import re
import shutil
import time

# radiod's descriptor: type, placement, direction, then a length -- e.g.
# cof1234, rib512, cdb1200. Placement is i (in place), o (out of place, input
# preserved) or d (out of place, input destroyed; radiod bc224260, 2026-10-07).
# Overridable, because this parses another program's log and that log's
# format is not a contract.
DEFAULT_PATTERN = r"\b([cr][iod][fb]\d+)\b"

# The two planners. fft-gen ships with radiod, reads radiod's own descriptors,
# and writes the version-named wisdom file radiod actually loads
# (wisdom-<fftwf_version>). fftwf-wisdom is FFTW's generic tool: it writes
# wherever -o says and has no way to express an input-destroying transform.
FFT_GEN = "fft-gen"
FFTW_WISDOM = "fftwf-wisdom"

MAX_ROUNDS = 8


class Miss:
    def __init__(self, spec, line):
        self.spec = spec
        self.line = line

    def __repr__(self):
        return "Miss(%r)" % (self.spec or self.line)


def read_log(env):
    """Transforms that fell back to ESTIMATE, as (misses, unparsed, exists)."""
    path = env.path("FFT_LOG")
    raw = env.read(path)
    if raw is None:
        return [], [], False
    pattern = re.compile(env.text("FFT_LOG_PATTERN", DEFAULT_PATTERN))
    misses, unparsed, seen = [], [], set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        specs = pattern.findall(line)
        if not specs:
            unparsed.append(line)
            continue
        for spec in specs:
            if spec in seen:
                continue
            seen.add(spec)
            misses.append(Miss(spec, line))
    return misses, unparsed, True


def log_predates(env, started):
    """True when fft.log was last written before radiod started.

    radiod appends on a miss and never rewrites the file, so a log older than
    the process holds only earlier runs' misses: this radiod has logged none.
    None when either time is unknown.
    """
    written = env.mtime(env.path("FFT_LOG"))
    if written is None or started is None:
        return None
    return written < started


def clear_log(env):
    """Truncate fft.log so the next look reports this boot's misses only.

    Without this the planner reads its own history and never converges.
    """
    path = env.path("FFT_LOG")
    if env.dry_run:
        return True, "dry-run: would truncate %s" % path
    if not env.exists(path):
        return True, "%s does not exist; nothing to clear" % path
    try:
        with open(path, "w"):
            pass
        return True, "truncated %s" % path
    except OSError as exc:
        return False, "could not truncate %s: %s" % (path, exc)


def planner(env):
    """FFT_GEN when it is installed, else FFTW_WISDOM. KA9Q_TUNE_PLANNER forces one."""
    forced = env.text("PLANNER", "")
    if forced in (FFT_GEN, FFTW_WISDOM):
        return forced
    command = env.command("FFT_GEN").split()
    return FFT_GEN if command and shutil.which(command[0]) else FFTW_WISDOM


def plan_command(env, specs):
    if planner(env) == FFT_GEN:
        # No -T: fft-gen names its output wisdom-<version>-threaded when given
        # threads, and radiod reads that file only when it runs threaded too.
        parts = [env.command("FFT_GEN"), "-v"]
    else:
        threads = env.number("WISDOM_THREADS", 0) or (os.cpu_count() or 1)
        parts = [env.command("FFTW_WISDOM"), "-v", "-T", str(threads),
                 "-o", env.path("WISDOM")]
    parts.extend(specs)
    return " ".join(parts)


def destroys_input(spec):
    return len(spec) > 1 and spec[1] == "d"


def restart_command(env, unit=None):
    """Restart the radiod that is actually running.

    The unit comes from radiod's cgroup. A packaged install runs
    ka9q-radio@<device>, which a radiod@* glob never matches: systemctl then
    restarts nothing and succeeds, and the planner reads an fft.log it has
    just emptied as convergence.
    """
    if env.overridden("RADIOD_RESTART") or not unit or "@" not in unit:
        return env.command("RADIOD_RESTART")
    return "systemctl restart %s.service" % unit


def converge(env, run_command, restart_command=None, settle_seconds=20,
             max_rounds=MAX_ROUNDS, sleep=time.sleep):
    """Plan what fft.log names until a full restart leaves it empty.

    run_command(cmd) -> (returncode, output) is injected so this is testable
    without planning a single transform.

    Returns (converged, [messages]).
    """
    restart = restart_command or env.command("RADIOD_RESTART")
    messages = []
    for round_number in range(1, max_rounds + 1):
        misses, unparsed, exists = read_log(env)
        if not exists:
            messages.append("%s does not exist: either radiod has not run, or "
                            "it logs elsewhere. Not the same as no misses."
                            % env.path("FFT_LOG"))
            return False, messages
        if unparsed:
            messages.append(
                "%d line(s) in fft.log did not match the transform pattern and "
                "were not planned; first: %s" % (len(unparsed), unparsed[0])
            )
        if not misses:
            messages.append("round %d: fft.log reports no ESTIMATE plans"
                            % round_number)
            return True, messages
        specs = [m.spec for m in misses]
        if planner(env) != FFT_GEN:
            stranded = [spec for spec in specs if destroys_input(spec)]
            if stranded:
                messages.append(
                    "%d input-destroying transform(s) (%s) can only be planned "
                    "by fft-gen, which is not installed; %s would plan the "
                    "input-preserving variant and radiod would still miss"
                    % (len(stranded), " ".join(stranded[:6]), FFTW_WISDOM))
                return False, messages
        messages.append("round %d: planning %d transform(s): %s"
                        % (round_number, len(specs), " ".join(specs)))
        if env.dry_run:
            # Nothing is planned or restarted, so a second round would only
            # read the same log again. Show the commands instead.
            messages.append("dry-run: would run %s" % plan_command(env, specs))
            messages.append("dry-run: would truncate %s, run %s, and repeat "
                            "until a restart leaves it empty"
                            % (env.path("FFT_LOG"), restart))
            return True, messages
        code, output = run_command(plan_command(env, specs))
        if code != 0:
            messages.append("wisdom planning failed (exit %d)%s"
                            % (code, (": " + output) if output else ""))
            return False, messages
        ok, detail = clear_log(env)
        messages.append(detail)
        if not ok:
            return False, messages
        code, output = run_command(restart)
        if code != 0:
            messages.append("radiod restart failed (exit %d)%s"
                            % (code, (": " + output) if output else ""))
            return False, messages
        sleep(settle_seconds)
    messages.append(
        "still finding ESTIMATE plans after %d rounds; stopping rather than "
        "looping. Something is regenerating transforms faster than they can be "
        "planned, or the wisdom file is not being read back." % max_rounds
    )
    return False, messages
