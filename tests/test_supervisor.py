import unittest
from pathlib import Path
import threading
from unittest.mock import Mock, patch
from mediaworker.supervisor import process_roots, supervise


class SupervisorInventoryTests(unittest.TestCase):
    def row(self, pid, parent):
        return {"ProcessId": pid, "ParentProcessId": parent}

    def test_venv_launcher_and_child_are_one_runtime(self):
        rows = [self.row(20, 10), self.row(10, 1)]
        self.assertEqual(process_roots(rows), [rows[1]])

    def test_orphaned_child_still_blocks_a_new_runtime(self):
        rows = [self.row(20, 10)]
        self.assertEqual(process_roots(rows), rows)

    def test_multiple_independent_runtimes_are_not_hidden(self):
        rows = [self.row(10, 1), self.row(20, 10), self.row(30, 1)]
        self.assertEqual(process_roots(rows), [rows[0], rows[2]])

    def test_cycles_and_duplicate_pids_fail_closed(self):
        for rows in ([self.row(10, 20), self.row(20, 10)], [self.row(10, 1), self.row(10, 1)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                process_roots(rows)

    def test_inventory_error_never_starts_a_worker(self):
        stop = threading.Event()
        with patch('mediaworker.supervisor.snapshot', side_effect=OSError), \
             patch('mediaworker.supervisor.subprocess.Popen') as start, \
             patch.object(stop, 'wait', side_effect=lambda _: stop.set()):
            supervise('run', Path('unused'), Path('unused'), Path('.'), stop)
        start.assert_not_called()

    def test_adoption_does_not_restart_an_existing_worker(self):
        stop = threading.Event()
        with patch('mediaworker.supervisor.snapshot', return_value=[self.row(20, 10)]), \
             patch('mediaworker.supervisor.wait_for_existing', side_effect=lambda *_: stop.set()), \
             patch('mediaworker.supervisor.subprocess.Popen') as start:
            supervise('run', Path('unused'), Path('unused'), Path('.'), stop)
        start.assert_not_called()

    def test_exited_child_is_restarted_after_a_fresh_inventory_check(self):
        stop = threading.Event()
        waits = 0

        def wait(seconds):
            nonlocal waits
            if seconds == 1:
                waits += 1
                if waits == 2:
                    stop.set()
            return stop.is_set()

        child = Mock(pid=10, returncode=7)
        child.poll.return_value = 7
        with patch('mediaworker.supervisor.snapshot', return_value=[]) as inventory, \
             patch('mediaworker.supervisor.subprocess.Popen', return_value=child) as start, \
             patch.object(stop, 'wait', side_effect=wait):
            supervise('run', Path('unused'), Path('unused'), Path('.'), stop)
        self.assertEqual(inventory.call_count, 2)
        self.assertEqual(start.call_count, 2)


if __name__ == "__main__":
    unittest.main()
