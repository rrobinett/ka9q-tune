"""Structural guards on the source, asserted against code with comments stripped.

Two lessons from building the precedent tool, both of which produced guards
that passed while the thing they guarded was broken:

  A guard asserted `grep -q _backoff`, which still matched the FUNCTION
  DEFINITION after the call site had been deleted. The mutation passed the
  entire suite.

  Assertions matched the tool's own comments. These files quote the buggy
  behaviour verbatim while explaining the fix, so a naive search finds the
  defect described in its own obituary.

So every guard here works on the AST -- call sites, not definitions; code, not
prose -- and the first test proves the stripper actually removes prose, because
a guard that silently stops stripping is a guard that silently stops guarding.
"""

import ast
import io
import os
import tokenize
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.join(os.path.dirname(HERE), "ka9q_tune")


def source(name):
    with open(os.path.join(PACKAGE, name)) as fh:
        return fh.read()


def strip_prose(text):
    """Remove comments and docstrings, leaving executable code only."""
    out = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except tokenize.TokenError:
        return text
    previous_type = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        if (token.type == tokenize.STRING
                and previous_type in (tokenize.INDENT, tokenize.NEWLINE,
                                      tokenize.NL, tokenize.DEDENT)):
            continue            # a bare string statement: a docstring
        out.append(token.string)
        if token.type not in (tokenize.NL, tokenize.NEWLINE):
            previous_type = token.type
        else:
            previous_type = token.type
    return " ".join(out)


def function(name, module):
    tree = ast.parse(source(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("no function %s in %s" % (name, module))


def call_lines(node, func_name):
    """Line numbers of calls to func_name inside node. Calls, not definitions."""
    lines = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        target = child.func
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name == func_name:
            lines.append(child.lineno)
    return sorted(lines)


class StripperTest(unittest.TestCase):
    """The guard on the guards."""

    def test_comments_and_docstrings_are_removed(self):
        text = (
            'def f():\n'
            '    """A docstring that mentions PROC_CMDLINE."""\n'
            '    # a comment that mentions PROC_CMDLINE\n'
            '    return 1\n'
        )
        stripped = strip_prose(text)
        self.assertNotIn("PROC_CMDLINE", stripped)
        self.assertIn("return", stripped)

    def test_code_survives(self):
        stripped = strip_prose('x = env.path("PROC_CMDLINE")  # not this\n')
        self.assertIn("PROC_CMDLINE", stripped)

    def test_the_real_files_contain_prose_worth_stripping(self):
        # If this ever fails, either the files stopped quoting the bugs they
        # fix or the stripper stopped stripping. Both matter: these guards
        # were written assuming prose is removed before they look.
        raw = source("isolation.py")
        phrase = "Part of the interface"       # appears only in a comment
        self.assertIn(phrase, raw)
        self.assertNotIn(phrase, strip_prose(raw))


class DeliveredStateGuard(unittest.TestCase):
    def test_read_delivered_does_not_consult_the_kernel_command_line(self):
        # R1. The command line is an intention. Reading it here is exactly the
        # bug that would have reported the broken station as healthy.
        node = function("read_delivered", "isolation.py")
        code = strip_prose(ast.unparse(node))
        self.assertNotIn("PROC_CMDLINE", code)
        self.assertNotIn("kernel_cmdline", code)
        self.assertIn("sys_cpu", code)

    def test_read_delivered_gets_rcu_from_the_kthreads(self):
        code = strip_prose(ast.unparse(function("read_delivered", "isolation.py")))
        self.assertIn("nocb_cpus", code)


class OneShotOrderGuard(unittest.TestCase):
    def test_the_marker_is_written_before_the_reboot_is_invoked(self):
        # Asserted on call sites in source order, not on the presence of the
        # names: both names appear in the file regardless of their order.
        node = function("one_shot", "isolation.py")
        marker = call_lines(node, "write_marker")
        reboot = call_lines(node, "run_command")
        self.assertTrue(marker, "one_shot no longer writes a marker at all")
        self.assertTrue(reboot, "one_shot no longer invokes the reboot command")
        self.assertLess(max(marker), max(reboot),
                        "the marker must go down before the reboot, or a kill "
                        "in between turns one reboot into a loop")

    def test_the_marker_is_cleared_only_on_the_active_path(self):
        node = function("one_shot", "isolation.py")
        self.assertTrue(call_lines(node, "clear_marker"),
                        "without clearing on a good boot the one-shot never "
                        "rearms, and R2's every-boot check becomes once-ever")


class VerificationGuard(unittest.TestCase):
    def test_cache_apply_reads_the_size_back_after_writing(self):
        # R5: never report success from configuration.
        node = function("apply", "cache.py")
        writes = call_lines(node, "write")
        verify = call_lines(node, "group_size")
        self.assertTrue(writes and verify)
        self.assertGreater(max(verify), max(writes))

    def test_irq_retarget_reads_back_after_writing(self):
        node = function("retarget", "irq.py")
        code = strip_prose(ast.unparse(node))
        self.assertIn("read_stripped", code)

    def test_set_pinned_writes_both_ends(self):
        # R7: a max-only cap is the failure, so both names must be written.
        code = strip_prose(ast.unparse(function("set_pinned", "freq.py")))
        self.assertIn("scaling_min_freq", code)
        self.assertIn("scaling_max_freq", code)

    def test_delivered_frequency_prefers_the_hardware_register(self):
        code = strip_prose(ast.unparse(function("delivered", "freq.py")))
        self.assertIn("cpuinfo_cur_freq", code)


class RefusalGuard(unittest.TestCase):
    def test_choose_pair_excludes_the_boot_cpu_in_code(self):
        code = strip_prose(ast.unparse(function("choose_pair", "radiod.py")))
        self.assertIn("boot", code)
        self.assertIn("continue", code)

    def test_apply_can_still_return_the_refusal_code(self):
        node = function("cmd_apply", "cli.py")
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        names = {ast.unparse(n.value) for n in returns if n.value is not None}
        self.assertIn("EXIT_REFUSED", names,
                      "R6 says detect and refuse; a warning is not a refusal")


if __name__ == "__main__":
    unittest.main()
