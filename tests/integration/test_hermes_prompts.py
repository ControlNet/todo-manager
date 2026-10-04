"""The cron prompts, the skill and gate output must pass the INSTALLED Hermes cron prompt scanner.

A scanner hit blocks the whole cron run, so this runs the real scanner (read-only) under the
Hermes interpreter instead of copying its patterns.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path

from todo_manager import hermes_install

HERMES_PY = Path(os.environ.get("TM_HERMES_PYTHON", "~/.hermes/hermes-agent/venv/bin/python")).expanduser()
HERMES_REPO = Path(os.environ.get("TM_HERMES_REPO", "~/.hermes/hermes-agent")).expanduser()
SCAN = """
import json, sys
sys.path.insert(0, sys.argv[1])
from tools.cronjob_prompt_scan import _scan_cron_prompt, _scan_cron_skill_assembled
req = json.loads(sys.stdin.read())
print(json.dumps({"prompt": [_scan_cron_prompt(p) for p in req["prompts"]],
                  "assembled": [_scan_cron_skill_assembled(a)[1] for a in req["assembled"]]}))
"""


@unittest.skipUnless(HERMES_PY.exists() and HERMES_REPO.exists(), "Hermes interpreter not available")
class HermesPromptScanTests(unittest.TestCase):
    def test_prompts_skill_and_gate_output_pass_the_scanner(self):
        skill = hermes_install.asset("skill/SKILL.md")
        prompts = [hermes_install.asset(f"cron/{e}.md") for e in hermes_install.JOBS]
        gate = ("Todo Manager feedback run: run_0000\nFeedback to interpret: 2 (comment 1, body_edit 1)\n"
                "Do: load skill todo-manager, run `todo-manager feedback show run_0000`, decide, then"
                " `todo-manager feedback apply run_0000 --file <decisions.json>`.\n{\"wakeAgent\": true}")
        assembled = [p + "\n\n" + skill + "\n\n" + gate for p in prompts]
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        proc = subprocess.run([str(HERMES_PY), "-c", SCAN, str(HERMES_REPO)], capture_output=True, text=True,
                              input=json.dumps({"prompts": prompts, "assembled": assembled}), env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(result, {"prompt": ["", ""], "assembled": ["", ""]})
