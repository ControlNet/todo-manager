"""One installation preview/confirmation, using existing deterministic setup steps.

Discovery uses a temporary local workspace. Only reads reach external systems until
all choices are known and the concrete combined change list has been confirmed.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

from . import config, db, install


def yes_no(picked):
    return None if picked[0] in ('yes', 'no') else 'choose yes or no'


class Choices:
    def __init__(self, user):
        self.user = user
        self.answers = {}
        self.changes = []

    def say(self, text):
        pass

    def choose(self, key, question, options, multiple=False, check=None):
        if key == 'email.accounts':
            # Discovery is temporary; email.scope is the single user-facing selection below.
            return [value for value, _ in options]
        values = self.user.choose(key, question, options, multiple, check)
        self.answers[key] = ','.join(values)
        return values

    def confirm(self, step, title, changes):
        self.changes.extend(changes)
        return False


class Preview(install.Install):
    def step_email(self):
        explicit = 'enabled' in self.explicit('email') or bool(self.explicit('email').get('accounts'))
        super().step_email()
        if not explicit and self.cfg().get('email.accounts'):
            options = [(a['name'], a.get('address') or a['name']) for a in self.cfg().get('email.accounts')]
            picked = self.prompter.choose('email.scope', 'Which discovered mailboxes may Todo Manager read?', options, multiple=True)
            selected = [a for a in self.cfg().get('email.accounts') if a['name'] in picked]
            self.write({'email.enabled': bool(selected), 'email.accounts': selected})

    def step_slack(self):
        explicit = 'enabled' in self.explicit('slack')
        super().step_slack()
        if not explicit and self.cfg().get('slack.enabled'):
            picked = self.prompter.choose('slack.scope', 'Which Slack conversation types may be read? Empty means none.',
                                         [('direct_messages','Direct messages'), ('group_direct_messages','Group messages'),
                                          ('channels','Joined channels')], multiple=True)
            self.write({'slack.enabled': bool(picked), **{f'slack.{k}': k in picked for k in
                        ('direct_messages','group_direct_messages','channels')}})

    def step_github(self):
        explicit = 'enabled' in self.explicit('github')
        super().step_github()
        if not explicit and self.cfg().get('github.enabled'):
            [answer] = self.prompter.choose('github.enabled', 'Read participating GitHub notification threads?',
                                           [('yes','Yes'), ('no','No')], check=yes_no)
            self.write({'github.enabled': answer == 'yes'})

    def step_kanban(self):
        if self.explicit('kanban').get('enabled') is False:
            [answer] = self.prompter.choose(
                'kanban.enabled',
                'Agent delegation is disabled. Source intake and Notion management remain available. Enable delegation?',
                [('yes', 'Configure and enable Agent delegation'), ('no', 'Keep delegation disabled')], check=yes_no)
            if answer == 'no':
                return
            self.write({'kanban.enabled': True})
        kb = self.env.kanban_client(self.cfg())
        if not kb.profile_valid(self.cfg().get('kanban.assignee')):
            def valid(picked):
                return None if picked[0] == 'disabled' or kb.profile_valid(picked[0]) else 'profile is not dispatchable'
            [profile] = self.prompter.choose('kanban.assignee', 'Execution profile (or disable delegations)?',
                                             [('default','Default Hermes profile'), ('disabled','Disable delegations')], check=valid)
            self.write({'kanban.assignee': '' if profile == 'disabled' else profile,
                        'kanban.enabled': profile != 'disabled'})
            if profile == 'disabled':
                return
        super().step_kanban()

    def step_hermes(self):
        actions = self.env.hermes_installer(self.cfg()).plan()
        conflicts = [a for a in actions if a.kind == 'conflict']
        if conflicts:
            raise install.InstallError('; '.join(a.summary for a in conflicts))
        self.prompter.changes.extend(f'{a.target}: {a.summary}' for a in actions if a.changes)

    def step_schedule(self):
        pass

    def _notion_setup(self, conn, client, cfg, home):
        # Identity is another real choice; discover it before any remote write.
        self._notion_identity(client)
        return super()._notion_setup(conn, client, cfg, home)


def complete(home, user, env=None, language=None, confirmed=False):
    env = env or install.Env()
    target = config.resolve_home(home)
    original = config.load(target)
    if original.db_path.exists():
        conn = sqlite3.connect(original.db_path.as_uri() + '?mode=ro', uri=True)
        try:
            columns = {r[1] for r in conn.execute('PRAGMA table_info(runs)')}
            if {'wake', 'released_at'} <= columns:
                active = conn.execute('SELECT id FROM runs WHERE wake=1 AND released_at IS NULL').fetchall()
                if active:
                    raise install.InstallError('finish or settle active runs using the previous release before switching CLI: '
                                               + ', '.join(r[0] for r in active))
        finally:
            conn.close()
    choices = Choices(user)
    with tempfile.TemporaryDirectory(prefix='todo-install-preview-') as tmp:
        scratch = Path(tmp) / 'workspace'
        scratch.mkdir()
        if (target / 'config.toml').exists():
            shutil.copy2(target / 'config.toml', scratch / 'config.toml')
        if original.db_path.exists():
            (scratch / 'state').mkdir()
            src = sqlite3.connect(original.db_path.as_uri() + '?mode=ro', uri=True)
            dst = sqlite3.connect(str(scratch / 'state' / 'todo.sqlite'))
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        # Hermes plans use the real destination, but read the discovered settings.
        preview_env = replace(env, installer=lambda cfg: env.hermes_installer(config.Config(target, cfg.data)))
        preview = Preview(scratch, choices, preview_env, language=language)
        preview.run()
        # Freeze absence as well as selected scope; later discovery must not silently enable a source.
        for source in ('email', 'slack', 'github'):
            if 'enabled' not in preview.explicit(source):
                preview.write({f'{source}.enabled': False})
        proposed = (scratch / 'config.toml').read_text()
        current = (target / 'config.toml').read_text() if (target / 'config.toml').exists() else ''
        changes = [f'initialize/upgrade local workspace {target}; archive legacy preferences; preserve native memory']
        if proposed != current:
            changes.append('save discovered source settings and selected identities in config.toml')
        discovered = preview.cfg()
        changes += [
            'email scope: ' + ', '.join(a['name'] for a in discovered.get('email.accounts') or [])
                if discovered.get('email.enabled') else 'email reading disabled',
            'Slack scope: ' + ', '.join(k for k in ('direct_messages','group_direct_messages','channels')
                                       if discovered.get(f'slack.{k}'))
                if discovered.get('slack.enabled') else 'Slack reading disabled',
            'GitHub participating notifications enabled' if discovered.get('github.enabled') else 'GitHub reading disabled',
            f"Agent delegation enabled; execution profile: {discovered.get('kanban.assignee') or 'none'}"
                if discovered.get('kanban.enabled') else
                'Agent delegation is disabled; source intake and Notion management remain available. Run todo-manager install to configure it.',
        ]
        changes.extend(choices.changes)
        # Installation does not resume previously paused jobs. New jobs need a real choice.
        from .hermes_install import load_jobs
        actual_inst = env.hermes_installer(config.Config(target, preview.cfg().data))
        missing = not load_jobs(actual_inst.paths)
        start = False
        if missing:
            [answer] = choices.choose('automation.start', 'Start the two new Todo cron jobs after installation?',
                                      [('yes', 'Start'), ('no', 'Keep paused')], check=yes_no)
            start = answer == 'yes'
            changes.append('start the two new cron jobs' if start else 'leave new cron jobs paused')
        if not confirmed:
            if not user.confirm('install', 'Install or upgrade Todo Manager:', changes):
                return {'status': 'declined', 'changes': changes}
        # All answers are cached; unresolved choices still fail, even with confirmation.
        prompter = install.AnswerPrompter(choices.answers, {'all'}, set() if start else {'schedule'}, output=lambda _: None)
        from . import export
        if original.db_path.exists():
            # A safety backup precedes schema/archive/config changes, even without a schema bump.
            saved = sqlite3.connect(str(original.db_path), isolation_level=None)
            saved.row_factory = sqlite3.Row
            try:
                export.backup(saved, original, prefix='pre-install')
            finally:
                saved.close()
        install.init_workspace(target, preview.cfg().language, preview.cfg().tz)
        config_path = target / 'config.toml'
        temp_config = target / 'config.toml.installing'
        temp_config.write_text(proposed, encoding='utf-8')
        temp_config.replace(config_path)
        actual = install.Install(target, prompter, env, language=language)
        actual.run()
        return {'status': 'complete', 'changes': changes, 'done': actual.report}
