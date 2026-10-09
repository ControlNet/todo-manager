"""Real Hermes APIs with synthetic boards/markers and no model or delivery."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.integration.test_native_contract import PYTHON, REPO
from todo_manager.kanban import BridgeKanban, KanbanError


@unittest.skipUnless(PYTHON.exists() and REPO.exists(), 'Hermes interpreter not available')
class NativeReadiness(unittest.TestCase):
    def test_archived_invalid_and_orphaned_boards_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix='tm-native-readiness-') as tmp:
            bridge = BridgeKanban('synthetic', str(PYTHON), str(REPO), tmp)
            self.assertEqual(bridge.ping()['board_state'], 'missing')
            self.assertFalse((Path(tmp) / 'kanban').exists())
            bridge.create_board('Synthetic', 'No workers')
            self.assertTrue(bridge.ping()['board_available'])
            env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_') and k not in ('PYTHONPATH','PYTHONHOME')}
            env.update(HERMES_HOME=tmp, HERMES_DISABLE_LAZY_INSTALLS='1')
            proc = subprocess.run([str(PYTHON), '-B', '-c',
                'import sys; sys.path.insert(0,sys.argv[1]); from hermes_cli import kanban_db as kb; '
                'kb.remove_board("synthetic", archive=True)', str(REPO)], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for op in (lambda: bridge.find('key', 'marker'), lambda: bridge.create_board('Again', 'No revival')):
                with self.assertRaises(KanbanError) as exc:
                    op()
                self.assertEqual(exc.exception.code, 'BOARD_ARCHIVED')
            before = {str(p): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
            info = bridge.ping()
            self.assertTrue(info['board_exists'])
            self.assertFalse(info['board_available'])
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()})
            metadata = Path(tmp) / 'kanban/boards/synthetic/board.json'
            metadata.write_text('{broken')  # Synthetic corruption only.
            self.assertEqual(bridge.ping()['board_state'], 'invalid')
            metadata.unlink()
            (metadata.parent / 'kanban.db').write_bytes(b'synthetic orphan')
            self.assertEqual(bridge.ping()['board_state'], 'orphaned')
            with self.assertRaises(KanbanError) as exc:
                bridge.create_board('Again', 'No adoption')
            self.assertEqual(exc.exception.code, 'BOARD_ORPHANED')

    def test_native_ticker_error_is_read_without_probe_or_repairs(self):
        import time
        with tempfile.TemporaryDirectory(prefix='tm-native-cron-read-') as tmp:
            bridge = BridgeKanban('unused', str(PYTHON), str(REPO), tmp)
            result = bridge.call('cron_health', mutating=False, active=True)
            self.assertEqual(result['state'], 'not_installed')
            self.assertEqual(list(Path(tmp).iterdir()), [])
            cron = Path(tmp) / 'cron'
            cron.mkdir()
            (cron / 'jobs.json').write_text('{broken synthetic input')
            (cron / 'ticker_heartbeat').write_text(str(time.time()))
            (cron / 'ticker_last_error').write_text(f'{time.time()}\nENOSPC: synthetic disk full\n')
            before = {str(p): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
            result = bridge.call('cron_health', mutating=False, active=True)
            self.assertEqual(result['state'], 'attention', result)
            self.assertIn('ENOSPC', ' '.join(result['problems']))
            self.assertIsNone(result['skipped_runs'])
            self.assertIsNone(result['writable'])
            self.assertFalse(result['write_probe'])
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()})
            paused = bridge.call('cron_health', mutating=False, active=False)
            self.assertEqual(paused['problems'], [])
            (cron / 'ticker_last_error').unlink()
            (cron / 'ticker_last_success').write_text(str(time.time()))
            recovered = bridge.call('cron_health', mutating=False, active=True)
            self.assertEqual(recovered['problems'], [])
