"""Migration proposal/apply on synthetic old-Notion pages (test fixtures, not real data)."""

import csv
import json

from todo_manager import migration, store
from tests.helpers import WorkspaceCase
from tests.test_notion import NotionCase

BOT = "bot-old"
USER = "user-old"


def page(pid, title, status, *, by=BOT, priority="P2", confidence="低", due=None, project=None,
         source_id="18c0ffee00000001", edited="2026-09-01T00:00:00.000Z"):
    def text(v):
        return {"type": "rich_text", "rich_text": [{"plain_text": v}] if v else []}
    return {"id": pid, "url": f"https://www.notion.so/{pid}", "created_time": "2026-08-01T00:00:00.000Z",
            "last_edited_time": edited, "created_by": {"id": BOT}, "last_edited_by": {"id": by}, "in_trash": False,
            "properties": {
                "任务名称": {"type": "title", "title": [{"plain_text": title}]},
                "状态": {"type": "status", "status": {"name": status}},
                "优先级": {"type": "select", "select": {"name": priority}},
                "置信度": {"type": "select", "select": {"name": confidence}},
                "截止日期": {"type": "date", "date": {"start": due} if due else None},
                "项目": {"type": "relation", "relation": [{"id": project}] if project else []},
                "来源类型": {"type": "select", "select": {"name": "gmail"}},
                "来源ID": text(source_id), "备注": text("旧系统自动备注"), "证据": text("邮件摘录"), "指纹": text("fp")}}


OLD_TASKS = [
    page("p-active", "等 Alice 回复数据集", "等待中", by=USER, project="proj-1"),
    page("p-done-user", "已回复 Bob", "已完成", by=USER, source_id="18c0ffee00000002"),
    page("p-done-bot", "系统自动完成", "已完成"),
    page("p-inbox-p1", "审稿截止", "收件箱", priority="P1"),
    page("p-inbox-noise", "某会议征稿", "收件箱"),
]
OLD_PROJECTS = [{"id": "proj-1", "url": "https://www.notion.so/proj-1", "properties": {
    "项目名称": {"type": "title", "title": [{"plain_text": "数据集论文"}]},
    "状态": {"type": "status", "status": {"name": "进行中"}}, "领域": {"type": "select", "select": {"name": "研究"}},
    "任务": {"type": "relation", "relation": [{"id": "p-active"}]}, "备注": {"type": "rich_text", "rich_text": []}}},
    {"id": "proj-2", "url": "", "properties": {
        "项目名称": {"type": "title", "title": [{"plain_text": "旧规划"}]},
        "状态": {"type": "status", "status": {"name": "规划中"}}, "领域": {"type": "select", "select": None},
        "任务": {"type": "relation", "relation": []}, "备注": {"type": "rich_text", "rich_text": []}}}]


class MigrationMixin:
    def make_proposal(self):
        root = self.cfg.home / "migration" / "t1"
        (root / "old").mkdir(parents=True)
        (root / "old" / "tasks.json").write_text(json.dumps({"results": OLD_TASKS}))
        (root / "old" / "projects.json").write_text(json.dumps({"results": OLD_PROJECTS}))
        (root / "old" / "meta.json").write_text(json.dumps({"bot": BOT, "fetched_at": "2026-10-04T00:00:00Z"}))
        summary = migration.propose(root, self.cfg)
        return root, summary

    def set_action(self, root, page_id, action):
        path = root / "tasks.csv"
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            if r["old_page_id"] == page_id:
                r["action"] = action
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, migration.TASK_COLUMNS)
            w.writeheader()
            w.writerows(rows)


class MigrationTests(MigrationMixin, WorkspaceCase):
    def test_proposal_classifies_and_apply_requires_decisions(self):
        root, summary = self.make_proposal()
        self.assertEqual(summary["by_category"], {"active": 1, "closed_by_user": 1, "closed_by_system": 1,
                                                  "inbox_candidate": 1, "inbox_noise": 1})
        self.assertEqual(summary["projects_kept"], 1)
        self.assertIn("review", (root / "summary.md").read_text())
        with self.assertRaises(ValueError):
            migration.preview(self.conn, self.cfg, root)
        self.set_action(root, "p-inbox-p1", "drop")
        report = migration.preview(self.conn, self.cfg, root)
        self.assertEqual((report["keep"], report["history"], report["drop"], report["projects"]), (1, 1, 3, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0, "preview writes nothing")

    def test_apply_imports_keep_and_history_once_and_links_threads(self):
        root, _ = self.make_proposal()
        self.set_action(root, "p-inbox-p1", "drop")
        resolve = {"18c0ffee00000001": "gmail:primary:t-a", "18c0ffee00000002": "gmail:primary:t-b"}.get
        report = migration.apply(self.conn, self.cfg, root, dry_run=False, resolve_thread=resolve)
        self.assertEqual(report["threads_linked"], 2)
        open_tasks = store.list_tasks(self.conn)
        self.assertEqual([(t["title"], t["status"], t["next_actor"]) for t in open_tasks],
                         [("等 Alice 回复数据集", "waiting", "external")])
        self.assertEqual(json.loads(open_tasks[0]["field_meta"])["status"]["by"], "human")
        self.assertEqual(store.get_project(self.conn, open_tasks[0]["project_id"])["title"], "数据集论文")
        closed = store.list_tasks(self.conn, ("done",))
        self.assertEqual([t["title"] for t in closed], ["已回复 Bob"])
        # A later mail in the thread the user closed is shown to the model as already handled.
        linked = store.tasks_for_threads(self.conn, ["gmail:primary:t-b"])
        self.assertEqual([t["status"] for t in linked], ["done"])
        again = migration.apply(self.conn, self.cfg, root, dry_run=False)
        self.assertEqual(again["skipped_existing"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)


class MigrationNotionTests(MigrationMixin, NotionCase):
    def test_history_is_not_pushed_but_kept_tasks_are(self):
        root, _ = self.make_proposal()
        self.set_action(root, "p-inbox-p1", "drop")
        migration.apply(self.conn, self.cfg, root, dry_run=False)
        self.sync = self.new_sync()
        self.sync.enqueue_dirty()
        self.drain()
        titles = {self.fake.prop(p["id"], "任务") for p in self.fake.pages_in(self.tasks_ds)}
        self.assertEqual(titles, {"等 Alice 回复数据集"})
