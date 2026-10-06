"""No model; real installed Hermes code, synthetic data, isolated HERMES_HOME."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

PYTHON = Path(os.environ.get('TM_HERMES_PYTHON', '~/.hermes/hermes-agent/venv/bin/python')).expanduser()
REPO = Path(os.environ.get('TM_HERMES_REPO', '~/.hermes/hermes-agent')).expanduser()


@unittest.skipUnless(PYTHON.exists() and REPO.exists(), 'Hermes interpreter not available')
class ReviewContract(unittest.TestCase):
    def test_review_reliability_on_real_native_code(self):
        with tempfile.TemporaryDirectory(prefix='tm-native-contract-') as home:
            env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_') and k not in ('PYTHONPATH', 'PYTHONHOME')}
            env['HERMES_HOME'] = home
            probe = Path(__file__).with_name('review_contract_probe.py')
            proc = subprocess.run([str(PYTHON), str(probe), str(REPO), str(probe.parents[2]), home],
                                  text=True, capture_output=True, env=env, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr[-5000:] + proc.stdout[-2000:])
            line = next(line for line in proc.stdout.splitlines() if line.startswith('@@PROBE@@'))
            results = json.loads(line[9:])
            self.assertEqual(len(results), 4)
            self.assertTrue(all(results.values()), results)
