"""The daemon's view of Hermes' cron execution ledger (``hermes_install.job_executing``) against
attempts written by the real installed Hermes code. No model, no job runs: a Hermes process in
an isolated temporary HERMES_HOME only writes ledger rows for a synthetic job id."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from todo_manager import config, hermes_install

PYTHON = Path(os.environ.get('TM_HERMES_PYTHON', '~/.hermes/hermes-agent/venv/bin/python')).expanduser()
REPO = Path(os.environ.get('TM_HERMES_REPO', '~/.hermes/hermes-agent')).expanduser()

# Owner of one attempt: claims it, marks it running, then finishes it (or exits without) on stdin.
OWNER = """
import sys
sys.path.insert(0, sys.argv[1])
from cron import executions as ex
e = ex.create_execution('job-synthetic', source='test')
ex.mark_execution_running(e['id'])
print('running', flush=True)
if sys.stdin.readline().strip() == 'finish':
    ex.finish_execution(e['id'], success=True)
print('done', flush=True)
"""


@unittest.skipUnless(PYTHON.exists() and REPO.exists(), 'Hermes interpreter not available')
class HermesExecutionLedger(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix='tm-hermes-ledger-')
        self.addCleanup(self.home.cleanup)
        hermes_home = Path(self.home.name) / 'hermes'
        hermes_home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_') and k not in ('PYTHONPATH', 'PYTHONHOME')}
        self.env['HERMES_HOME'] = str(hermes_home)
        cfg = config.load(Path(self.home.name) / 'workspace', overrides={'hermes': {'home': str(hermes_home)}})
        self.paths = hermes_install.Paths.from_config(cfg)

    def owner(self):
        proc = subprocess.Popen([str(PYTHON), '-c', OWNER, str(REPO)], env=self.env, cwd=self.home.name, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        line = proc.stdout.readline()
        self.assertEqual(line.strip(), 'running', proc.stderr.read() if proc.poll() is not None else line)
        return proc

    def end(self, proc, how):
        out, err = proc.communicate(f'{how}\n', timeout=60)
        self.assertEqual(proc.returncode, 0, err[-3000:])

    def test_a_running_attempt_counts_until_hermes_finishes_it(self):
        self.assertIsNone(hermes_install.job_executing(self.paths, 'job-synthetic'), 'no ledger yet')
        proc = self.owner()
        self.assertTrue(hermes_install.job_executing(self.paths, 'job-synthetic'))
        self.assertFalse(hermes_install.job_executing(self.paths, 'another-job'))
        self.end(proc, 'finish')
        self.assertFalse(hermes_install.job_executing(self.paths, 'job-synthetic'))

    def test_an_attempt_whose_owner_died_does_not_count(self):
        proc = self.owner()
        self.end(proc, 'exit')        # gone without finishing: left `running` in the ledger
        self.assertFalse(hermes_install.job_executing(self.paths, 'job-synthetic'))


if __name__ == '__main__':
    sys.exit(unittest.main())
