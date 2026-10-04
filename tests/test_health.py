from todo_manager import clock, gate_feedback, health
from todo_manager.sources.base import FetchResult, persist_fetch, record_fetch_error
from tests.helpers import WorkspaceCase


class SourceStalenessTests(WorkspaceCase):
    def stale_lines(self):
        return [p for p in health.report(self.conn, self.cfg)["problems"] if p.startswith("source ")]

    def test_a_polling_interval_widens_the_stale_threshold(self):
        persist_fetch(self.conn, "slack", FetchResult(scope="conversation:Q", items=[], cursor="c",
                                                      meta={"poll_interval_minutes": 30}))
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:x", items=[], cursor="c"))
        clock.advance(minutes=45)
        self.assertEqual([p.split(" stale")[0] for p in self.stale_lines()], ["source gmail:mailbox:x"])
        clock.advance(minutes=20)
        self.assertEqual(len(self.stale_lines()), 2)

    def test_many_failing_scopes_of_one_source_are_reported_once(self):
        for i in range(6):
            record_fetch_error(self.conn, "slack", f"conversation:D{i}", "slack conversations.history: invalid_auth")
        record_fetch_error(self.conn, "gmail", "mailbox:x", "HTTP 503")
        lines = self.stale_lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(any(line.startswith("source slack: 6 scopes stale") for line in lines))
        home = gate_feedback.freshness_sections(self.conn, self.cfg, "en")["extra"]
        slack = [label for label, _ in home if label.startswith("⚠️ Slack")]
        self.assertEqual(slack, ["⚠️ Slack (6 scopes)"])
