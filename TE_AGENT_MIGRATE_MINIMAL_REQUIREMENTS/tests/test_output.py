import io
import threading
import unittest

from te_agent_migrate.models import StepStatus
from te_agent_migrate.output import MigrationConsole


FAKE_TOKEN = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # 32 chars, synthetic test value


def make_console(verbose=False):
    stream = io.StringIO()
    return MigrationConsole(verbose=verbose, stream=stream, use_color=False), stream


def lines(stream):
    return [line for line in stream.getvalue().splitlines() if line.strip()]


class TaskHeaderTests(unittest.TestCase):
    def test_play_header_and_mode_line(self):
        console, stream = make_console()
        console.play("Migrate agents", "dry-run", "D")
        out = lines(stream)
        self.assertTrue(out[0].startswith("PLAY [Migrate agents] ****"))
        self.assertEqual(len(out[0]), 72)
        self.assertEqual(out[1], "MODE: DRY-RUN   STRATEGY: D")

    def test_task_header_width_and_once_in_a_row(self):
        console, stream = make_console()
        console.task("preflight")
        console.task("preflight")
        out = lines(stream)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("TASK [preflight] ****"))
        self.assertEqual(len(out[0]), 72)

    def test_task_repeats_after_other_task(self):
        console, stream = make_console()
        console.task("a")
        console.task("b")
        console.task("a")
        headers = [line for line in lines(stream) if line.startswith("TASK [")]
        self.assertEqual([h.split("]")[0] for h in headers], ["TASK [a", "TASK [b", "TASK [a"])

    def test_status_announces_task_automatically(self):
        console, stream = make_console()
        console.status(StepStatus.OK, "agent1", "register")
        console.status(StepStatus.OK, "agent2", "register")
        console.status(StepStatus.CHANGED, "agent1", "verify", "done")
        out = lines(stream)
        self.assertTrue(out[0].startswith("TASK [register]"))
        self.assertTrue(out[1].startswith("ok:"))
        self.assertTrue(out[2].startswith("ok:"))
        self.assertTrue(out[3].startswith("TASK [verify]"))
        self.assertTrue(out[4].startswith("changed:"))
        self.assertEqual(sum(1 for line in out if line.startswith("TASK [")), 2)


class HostLineTests(unittest.TestCase):
    def host_lines(self, status, detail=""):
        console, stream = make_console()
        console.status(status, "h1", "step", detail)
        return lines(stream)[1]

    def test_formats_for_every_status(self):
        expected = {
            StepStatus.OK: ("ok:", ""),
            StepStatus.CHANGED: ("changed:", " => why"),
            StepStatus.SKIPPED: ("skipped:", " => why"),
            StepStatus.FAILED: ("failed:", " => why"),
            StepStatus.PAUSED: ("paused:", " => why"),
        }
        for status, (label, suffix) in expected.items():
            detail = "" if status is StepStatus.OK else "why"
            line = self.host_lines(status, detail)
            self.assertTrue(line.startswith(label), line)
            self.assertTrue(line.endswith("[h1]" + suffix), line)

    def test_labels_are_aligned(self):
        columns = set()
        for status in StepStatus:
            line = self.host_lines(status, "x")
            columns.add(line.index("[h1]"))
        self.assertEqual(len(columns), 1)

    def test_localhost_mapping(self):
        console, stream = make_console()
        console.status(StepStatus.OK, "global", "inventory", "loaded 3")
        console.status(StepStatus.OK, None, "inventory2")
        out = lines(stream)
        self.assertIn("[localhost] => loaded 3", out[1])
        self.assertTrue(out[3].endswith("[localhost]"))

    def test_info_format(self):
        console, stream = make_console()
        console.info("run", "starting")
        self.assertEqual(stream.getvalue(), "  [info] run: starting\n")

    def test_progress_silent_when_not_verbose(self):
        console, stream = make_console(verbose=False)
        console.progress("h1", "step", "working")
        self.assertEqual(stream.getvalue(), "")

    def test_progress_verbose_format(self):
        console, stream = make_console(verbose=True)
        console.progress("h1", "step", "working")
        console.progress("global", "inv")
        out = stream.getvalue().splitlines()
        self.assertEqual(out[0], "  .. [h1] step - working")
        self.assertEqual(out[1], "  .. [localhost] inv - in progress")

    def test_debug_only_verbose(self):
        console, stream = make_console(verbose=False)
        console.debug("hidden")
        self.assertEqual(stream.getvalue(), "")


class RecapTests(unittest.TestCase):
    def build(self):
        console, stream = make_console()
        console.status(StepStatus.OK, "short", "s1")
        console.status(StepStatus.CHANGED, "short", "s2", "c")
        console.status(StepStatus.SKIPPED, "a-much-longer-host", "s2", "k")
        console.status(StepStatus.FAILED, "a-much-longer-host", "s3", "boom")
        return console, stream

    def test_recap_counts_and_alignment(self):
        console, stream = self.build()
        stream.truncate(0)
        stream.seek(0)
        console.recap(None)
        out = lines(stream)
        self.assertTrue(out[0].startswith("PLAY RECAP ****"))
        self.assertEqual(len(out[0]), 72)
        host_lines = out[1:]
        self.assertEqual(len(host_lines), 2)
        self.assertTrue(host_lines[0].startswith("a-much-longer-host : "))
        self.assertTrue(host_lines[1].startswith("short"))
        self.assertEqual(host_lines[0].index(":"), host_lines[1].index(":"))
        self.assertIn("ok=0    changed=0    skipped=1    failed=1", host_lines[0])
        self.assertIn("ok=1    changed=1    skipped=0    failed=0", host_lines[1])
        self.assertNotIn("paused", stream.getvalue())

    def test_paused_shown_only_when_nonzero_and_not_skipped(self):
        console, stream = make_console()
        console.status(StepStatus.PAUSED, "h1", "s")
        console.recap([])
        text = stream.getvalue()
        self.assertIn("skipped=0", text)
        self.assertIn("paused=1", text)
        self.assertEqual(console.counters()["h1"]["paused"], 1)

    def test_recap_rows_summary(self):
        console, stream = make_console()
        console.status(StepStatus.OK, "h1", "s")
        console.recap([{"agent": "h1", "status": "ok", "detail": "fine"}])
        text = stream.getvalue()
        self.assertIn("Run summary", text)
        self.assertIn("h1 | ok | fine", text)

    def test_recap_rows_status_enum(self):
        console, stream = make_console()
        console.recap([{"agent": "h1", "status": StepStatus.FAILED, "detail": "bad"}])
        self.assertIn("h1 | failed | bad", stream.getvalue())

    def test_has_failures(self):
        console, _ = make_console()
        self.assertFalse(console.has_failures())
        console.status(StepStatus.OK, "h1", "s")
        console.status(StepStatus.PAUSED, "h1", "s")
        self.assertFalse(console.has_failures())
        console.status(StepStatus.FAILED, "global", "run", "x")
        self.assertTrue(console.has_failures())


class ThreadSafetyTests(unittest.TestCase):
    def test_parallel_status_calls(self):
        console, stream = make_console()
        per_thread = 50
        threads = []

        def worker(index):
            for n in range(per_thread):
                console.status(StepStatus.OK, "host%d" % index, "step%d" % (n % 3))

        for index in range(5):
            thread = threading.Thread(target=worker, args=(index,))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        counters = console.counters()
        self.assertEqual(sorted(counters), ["host%d" % i for i in range(5)])
        for counts in counters.values():
            self.assertEqual(counts["ok"], per_thread)
        out = stream.getvalue().splitlines()
        host_lines = [line for line in out if line.startswith("ok:")]
        self.assertEqual(len(host_lines), 5 * per_thread)
        for line in host_lines:
            self.assertRegex(line, r"^ok:\s+\[host\d\]$")


class SecretMaskingTests(unittest.TestCase):
    def test_bearer_and_token_masked_in_status(self):
        console, stream = make_console()
        console.status(
            StepStatus.FAILED,
            "h1",
            "s",
            "header Bearer abc.def-123 and token %s leaked" % FAKE_TOKEN,
        )
        text = stream.getvalue()
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("abc.def-123", text)
        self.assertIn("[REDACTED]", text)

    def test_masked_in_info_debug_progress_recap(self):
        console, stream = make_console(verbose=True)
        console.info("x", "tok %s" % FAKE_TOKEN)
        console.debug("Bearer secretvalue")
        console.progress("h1", "s", FAKE_TOKEN)
        console.recap([{"agent": "h1", "status": "failed", "detail": FAKE_TOKEN}])
        text = stream.getvalue()
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("secretvalue", text)


class BannerTests(unittest.TestCase):
    def test_destination_banner_prints_mode(self):
        class Group(object):
            def __init__(self, name, aid):
                self.name = name
                self.aid = aid

        class Mode(object):
            value = "dry-run"

        console, stream = make_console()
        console.destination_banner(Group("src", "1"), Group("dst", "2"), Mode())
        self.assertIn("MODE: DRY-RUN", stream.getvalue())

    def test_no_color_when_not_tty(self):
        stream = io.StringIO()
        console = MigrationConsole(stream=stream)
        console.status(StepStatus.OK, "h1", "s")
        self.assertNotIn("\033[", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
