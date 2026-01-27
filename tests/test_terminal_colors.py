import io
import json
import unittest
from contextlib import redirect_stdout

from qsync.terminal_colors import (
    Colors,
    colored,
    colorize_unified_diff_lines,
    get_color_mode,
    set_color_mode,
)


class TerminalColorPolicyTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_color_mode("auto")

    def test_color_mode_never_disables_ansi(self) -> None:
        set_color_mode("never")
        rendered = colored("hello", Colors.RED)
        self.assertEqual(rendered, "hello")

    def test_color_mode_always_enables_ansi(self) -> None:
        set_color_mode("always")
        rendered = colored("hello", Colors.RED)
        self.assertIn(Colors.RED, rendered)
        self.assertIn(Colors.RESET, rendered)

    def test_unified_diff_no_ansi_when_disabled(self) -> None:
        set_color_mode("never")
        lines = ["@@ -1,2 +1,2 @@", "-old", "+new", " context"]
        rendered = colorize_unified_diff_lines(lines)
        self.assertEqual(rendered, lines)

    def test_doctor_json_has_no_ansi_even_with_color_always(self) -> None:
        from qsync.cli import main

        prev_mode = get_color_mode()
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    main(["--color", "always", "doctor", "--json"])
                except SystemExit:
                    # doctor may exit with code 2 if workspace is invalid;
                    # we're just checking for ANSI codes in the JSON output.
                    pass
            output = buf.getvalue()
            json.loads(output)
            self.assertNotIn("\x1b", output)
        finally:
            set_color_mode(prev_mode)


if __name__ == "__main__":
    unittest.main()
