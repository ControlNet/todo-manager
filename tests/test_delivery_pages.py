"""Each delivered version as a page under its task page (2026-10-12): the user reads deliveries in
Notion and cannot open files on the server. Text files become page text, PDFs and images are
uploaded and shown inline, the agent's own records (JSON, archives) are listed as kept on the
server. Kanban is ``FakeKanban`` and Notion is ``FakeNotion`` (test doubles).
"""

from todo_manager import delegation
from todo_manager.notion.client import NotionUncertain
from tests.test_agent_board import Board


class DeliveryPageTests(Board):
    def deliver(self, files, summary="草稿在附件", notes="## 结论\n\n| 页 | 建议 |\n|---|---|\n| 9 | 删除 |"):
        did, card = self.delegated()
        folder = self.tmp / "worker"
        folder.mkdir(exist_ok=True)
        paths = []
        for name, data in files.items():
            (folder / name).write_bytes(data)
            paths.append(str(folder / name))
        self.kb.worker_start(card)
        self.kb.worker_review(card, summary, {"deliverable_markdown": notes, "artifacts": paths})
        self.tick()
        self.tick()
        return did

    def delivery_page(self):
        [block] = [b for b in self.fake.blocks.get(self.page, []) if b["type"] == "child_page"]
        return block

    def test_files_are_readable_in_notion(self):
        self.deliver({"review.md": "# 审核\n\n- 删除第 2 页".encode(), "email.txt": b"Hi Alice,\n\nPlease find...",
                      "draft.pdf": b"%PDF-1.7 draft", "check.json": b"{}", "sources.zip": b"PK"})
        child = self.delivery_page()
        self.assertTrue(child["child_page"]["title"].startswith("交付 v1 · "))
        text = self.fake.markdown[child["id"]]
        self.assertIn("# 审核", text, "Markdown stays Markdown")
        self.assertIn("```text\nHi Alice,", text, "plain text in a copyable box")
        self.assertIn("| 9 | 删除 |", text, "the agent's notes in full")
        self.assertIn("仅保存在服务器上", text)
        self.assertIn("check.json", text)
        self.assertIn("sources.zip", text)
        blocks = self.fake.blocks[child["id"]]
        pdfs = [b for b in blocks if b["type"] == "pdf"]
        self.assertEqual(len(pdfs), 1)
        upload = self.fake.uploads[pdfs[0]["pdf"]["file_upload"]["id"]]
        self.assertEqual((upload["filename"], upload["status"]), ("draft.pdf", "attached"))
        self.assertEqual(len(self.fake.uploads), 1, "only the PDF is uploaded")

    def test_the_agent_box_links_to_the_version_page(self):
        self.deliver({"draft.pdf": b"%PDF"})
        page_md = self.fake.markdown[self.page]
        self.assertIn("打开交付 v1", page_md)
        self.assertIn(self.fake.pages[self.delivery_page()["id"]]["url"], page_md)
        self.assertNotIn("展开交付内容", page_md, "the full text is on the version page")

    def test_a_file_over_the_upload_limit_stays_on_the_server(self):
        self.fake.max_upload_bytes = 10
        self.deliver({"big.pdf": b"%PDF" + b"x" * 100})
        child = self.delivery_page()
        self.assertEqual(self.fake.uploads, {})
        self.assertIn("big.pdf", self.fake.markdown[child["id"]])
        self.assertIn("超过 Notion 上传上限", self.fake.markdown[child["id"]])

    def test_a_retry_after_a_lost_append_does_not_add_the_file_twice(self):
        original = self.client.append_children
        calls = {"n": 0}

        def flaky(block_id, children, position=None):
            result = original(block_id, children, position)
            calls["n"] += 1
            if calls["n"] == 1:
                raise NotionUncertain("timeout after the append")
            return result

        self.client.append_children = flaky
        self.deliver({"a.pdf": b"%PDF a", "b.png": b"\x89PNG b"})
        self.client.append_children = original
        from todo_manager import clock
        clock.advance(minutes=10)
        self.tick()
        blocks = self.fake.blocks[self.delivery_page()["id"]]
        self.assertEqual([b["type"] for b in blocks if b["type"] in ("pdf", "image")], ["pdf", "image"])

    def test_each_version_has_its_own_page(self):
        did = self.deliver({"v1.pdf": b"%PDF 1"})
        from todo_manager import clock
        clock.advance(minutes=30)
        delegation.add_message(self.conn, did, "revision", "第二段改短", feedback_id="fbk_rev")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "改好了", {"artifacts": []})
        self.tick()
        self.tick()
        titles = [b["child_page"]["title"] for b in self.fake.blocks[self.page] if b["type"] == "child_page"]
        self.assertEqual([t.split(" · ")[0] for t in titles], ["交付 v1", "交付 v2"])
        self.assertIn("打开交付 v2", self.fake.markdown[self.page])

    def test_a_file_kept_twice_is_shown_once(self):
        did = self.deliver({"draft.pdf": b"%PDF"})
        from todo_manager.db import dumps, loads
        row = self.conn.execute("SELECT id, artifacts FROM deliveries WHERE delegation_id=?", (did,)).fetchone()
        arts = loads(row["artifacts"])
        self.conn.execute("UPDATE deliveries SET artifacts=? WHERE id=?",
                          (dumps(arts + [{**arts[0], "path": arts[0]["path"].replace(".pdf", "-2.pdf")}]), row["id"]))
        from todo_manager.notion import delivery_pages
        files = delivery_pages.sort_files(self.cfg.home, delivery_pages._artifacts(
            dict(self.conn.execute("SELECT * FROM deliveries WHERE id=?", (row["id"],)).fetchone())), 10 ** 6)
        self.assertEqual([m["name"] for m in files["media"]], ["draft.pdf"])
