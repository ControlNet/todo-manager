"""Real native cron CLI; temporary homes, no scheduler/model/network delivery."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from todo_manager import config, hermes_install
from todo_manager.install import init_workspace
from tests.integration.test_native_contract import PYTHON, REPO


@unittest.skipUnless(PYTHON.exists() and REPO.exists(), 'Hermes interpreter not available')
class NativeCliInstall(unittest.TestCase):
    def test_native_cron_and_generated_prerun_upgrade_pause_resume_uninstall(self):
        with tempfile.TemporaryDirectory(prefix='tm-cli-native-') as tmp:
            root = Path(tmp)
            home = root / 'workspace'
            init_workspace(home, 'en')
            cfg = config.load(home, overrides={'hermes': {'home': str(root / 'hermes')}})
            paths = hermes_install.Paths(root / 'hermes', home, root / 'bin/todo-manager', python=str(PYTHON))

            def runner(cmd, **kw):
                kw['env']['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
                return subprocess.run([str(PYTHON), '-m', 'hermes_cli.main', *cmd[1:]], cwd=REPO, **kw)

            inst = hermes_install.Installer(cfg, paths, runner=runner)
            inst.apply(inst.plan())
            jobs = hermes_install.load_jobs(paths)
            self.assertEqual(len(jobs), 2)
            self.assertTrue(all(j['state'] == 'paused' for j in jobs.values()))
            with patch.object(hermes_install.Paths, 'from_config', return_value=paths):
                reported = hermes_install.status(cfg)
            self.assertEqual(reported['state'], 'paused', reported)
            self.assertEqual(reported['problems'], [])
            env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_') and k not in ('PYTHONPATH','PYTHONHOME')}
            env.update(HERMES_HOME=str(paths.hermes_home), HERMES_DISABLE_LAZY_INSTALLS='1')
            for entry in ('intake','feedback'):
                result = subprocess.run([str(PYTHON), str(paths.script(entry))], env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'wakeAgent': False})
            inst.set_jobs_state('resume')
            self.assertTrue(all(j['state'] != 'paused' for j in hermes_install.load_jobs(paths).values()))
            with patch.object(hermes_install.Paths, 'from_config', return_value=paths):
                self.assertEqual(hermes_install.status(cfg)['state'], 'ready')
            inst.set_jobs_state('pause')
            (root / 'user-workdir').mkdir()
            for job in jobs.values():
                inst.hermes('cron', 'edit', job['id'], '--workdir', str(root / 'user-workdir'))
            inst.apply(inst.plan())
            self.assertTrue(all(j['state'] == 'paused' and j['workdir'] == str(root / 'user-workdir')
                                for j in hermes_install.load_jobs(paths).values()))
            inst.uninstall(inst.uninstall_plan())
            self.assertEqual(hermes_install.load_jobs(paths), {})
            self.assertTrue(cfg.db_path.exists())
