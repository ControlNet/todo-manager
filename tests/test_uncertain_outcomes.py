"""An external write whose outcome is unknown is reconciled, never replayed blindly.

* The outbox keeps an effect ``uncertain`` until a reconciling attempt settles it: a failed
  lookup (``Retryable`` or any error) leaves it uncertain, so the next attempt reconciles again.
* ``net.request_json`` reports a dropped connection, a truncated body or an unreadable body
  as a possibly-sent request (``HttpTimeout``): reads are retried, writes become uncertain.

Synthetic tasks, comments and a local HTTP stand-in only; no real Notion."""
import http.server
import threading
from unittest import mock

from tests.fake_notion import BOT_ID
from tests.helpers import WorkspaceCase
from tests.test_notion import NotionCase
from todo_manager import clock, effects, outbox, store
from todo_manager.net import HttpError, HttpTimeout, request_json
from todo_manager.notion.client import NotionClient, NotionError, NotionUncertain


class OutboxReconcileTests(WorkspaceCase):
    """Synthetic handlers: which outcomes keep an uncertain effect uncertain."""

    def setUp(self):
        super().setUp()
        self.oid = outbox.enqueue(self.conn, target='matrix', op='synthetic_write', dedupe_key='w1', payload={})
        self.calls = []

    def run_with(self, *outcomes):
        """Process the effect once per outcome (an exception to raise or a result), returning statuses."""
        statuses = []
        for outcome in outcomes:
            def handler(item, reconcile, outcome=outcome):
                self.calls.append(reconcile)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            outbox.process(self.conn, {('matrix', 'synthetic_write'): handler})
            statuses.append(self.status())
            clock.advance(hours=2)
        return statuses

    def status(self):
        return self.conn.execute('SELECT status FROM outbox WHERE id=?', (self.oid,)).fetchone()['status']

    def test_a_failed_reconcile_keeps_the_effect_uncertain(self):
        statuses = self.run_with(outbox.Uncertain('lost response'), outbox.Retryable('not visible yet', retry_after=60),
                                 RuntimeError('lookup failed'), {'ok': True})
        self.assertEqual(statuses, ['uncertain', 'uncertain', 'uncertain', 'done'])
        self.assertEqual(self.calls, [False, True, True, True], 'every later attempt reconciles first')

    def test_a_reconcile_that_finds_the_effect_impossible_ends_it(self):
        self.assertEqual(self.run_with(outbox.Uncertain('lost'), outbox.Permanent('target deleted')),
                         ['uncertain', 'dead'])

    def test_an_unsettled_reconcile_still_gives_up_at_the_attempt_limit(self):
        statuses = self.run_with(outbox.Uncertain('lost'), *[RuntimeError('lookup failed')] * outbox.MAX_ATTEMPTS)
        self.assertEqual(statuses[outbox.MAX_ATTEMPTS - 2], 'uncertain')
        self.assertEqual(statuses[outbox.MAX_ATTEMPTS - 1:], ['dead'] * 2, 'given up, and never run again')
        self.assertEqual(len(self.calls), outbox.MAX_ATTEMPTS)
        self.assertNotIn(False, self.calls[1:], 'never replayed without reconciling')

    def test_a_failure_before_any_uncertainty_still_fails(self):
        self.assertEqual(self.run_with(outbox.Retryable('rate limited'), {'ok': True}), ['failed', 'done'])
        self.assertEqual(self.calls, [False, False])


class LostWriteReconcileTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, 'Synthetic follow-up', origin='intake')

    def row(self, op, rowid=0):
        return self.conn.execute('SELECT * FROM outbox WHERE op=? ORDER BY rowid LIMIT 1 OFFSET ?',
                                 (op, rowid)).fetchone()

    def bot_comments(self):
        return [c for c in self.fake.comments if c['created_by']['id'] == BOT_ID]

    def test_failed_comment_lookup_does_not_post_the_comment_again(self):
        self.push(self.task)
        self.fake.lose_response.add(('POST', '/comments'))
        effects.task_comment(self.conn, self.task, 'k1', 'Synthetic delivery update')
        self.drain()
        self.assertEqual(self.row('comment')['status'], 'uncertain')
        clock.advance(minutes=2)
        self.fake.fail_next[('GET', '/comments')] = HttpError(400, 'synthetic lookup failure')
        self.drain()
        self.assertEqual(self.row('comment')['status'], 'uncertain', 'the lookup failed: delivery is still unknown')
        clock.advance(hours=2)
        self.drain()
        self.assertEqual(len(self.bot_comments()), 1, 'found by its marker, not posted again')
        self.assertEqual(self.row('comment')['status'], 'done')

    def test_failed_lookup_of_a_lost_create_holds_newer_pushes(self):
        hidden = set()
        query = self.fake._query

        def lagging(ds, body):   # a page Notion has created but does not list yet
            data = query(ds, body)
            return {**data, 'results': [p for p in data['results'] if p['id'] not in hidden]}
        self.fake._query = lagging
        self.fake.lose_response.add(('POST', '/pages'))
        self.assertEqual(self.push(self.task)['uncertain'], 1)
        hidden.update(p['id'] for p in self.fake.pages_in(self.tasks_ds))
        clock.advance(minutes=2)
        self.fake.fail_next[('POST', f'/data_sources/{self.tasks_ds}/query')] = HttpError(400, 'synthetic query failure')
        self.drain()
        self.assertEqual(self.row('push_task')['status'], 'uncertain')
        store.update_task(self.conn, self.task, {'summary': 'Changed meanwhile'}, actor='human', origin='cli',
                          based_on_version=store.get_task(self.conn, self.task)['version'])
        self.assertIsNotNone(effects.task_push(self.conn, self.task))
        self.drain()
        self.assertEqual(self.row('push_task')['status'], 'uncertain', 'not superseded by the newer push')
        self.assertEqual(self.row('push_task', 1)['status'], 'pending', 'waits for the lost create to settle')
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 1, 'no second page while the first is unlisted')
        hidden.clear()
        clock.advance(hours=2)
        self.drain()
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 1)
        self.assertIsNotNone(self.page_of(self.task))
        self.drain()   # the newer push became due once the older one settled
        self.assertEqual([self.row('push_task', i)['status'] for i in (0, 1)], ['done', 'done'])
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 1)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


class _Handler(http.server.BaseHTTPRequestHandler):
    """``/drop`` closes without answering, ``/truncated`` cuts the body short, ``/html`` answers
    200 with a non-JSON body, ``/error-truncated`` cuts a 500 body short. A path may succeed
    after its first ``fail`` requests (``?fail=1``)."""

    def log_message(self, *args):
        pass

    def _handle(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        path, _, query = self.path.partition('?')
        server = self.server
        server.requests.append((self.command, path))
        fail = int(dict(p.split('=') for p in query.split('&') if p).get('fail', 10 ** 6))
        if sum(1 for r in server.requests if r[1] == path) > fail:
            return self._send(200, b'{"object": "ok"}')
        if path == '/drop':
            self.close_connection = True
            return
        if path == '/truncated':
            return self._send(200, b'{"object": "comm', length=100)
        if path == '/html':
            return self._send(200, b'<html>proxy error</html>', content_type='text/html')
        if path == '/error-truncated':
            return self._send(500, b'{"object": "err', length=100)

    def _send(self, status, data, length=None, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length if length is not None else len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True

    do_GET = do_POST = do_PATCH = _handle


class IncompleteResponseTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.server = _Server(('127.0.0.1', 0), _Handler)
        self.server.requests = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = NotionClient(transport=self.transport, min_interval=0)

    def transport(self, method, path, params, body):
        return request_json(method, self.base + path, params=params, body=body, timeout=5)

    def test_a_response_that_never_completes_is_a_possibly_sent_request(self):
        for path in ('/drop', '/truncated', '/html'):
            with self.subTest(path=path), self.assertRaises(HttpTimeout):
                request_json('POST', self.base + path, body={'x': 1}, timeout=5)

    def test_an_error_status_with_a_truncated_body_keeps_its_status(self):
        with self.assertRaises(HttpError) as caught:
            request_json('GET', self.base + '/error-truncated', timeout=5)
        self.assertEqual(caught.exception.status, 500)

    def test_a_write_without_a_complete_response_is_uncertain(self):
        for path in ('/drop', '/truncated', '/html'):
            with self.subTest(path=path), self.assertRaises(NotionUncertain):
                self.client.post(path, {'markdown': 'Synthetic comment'})
        self.assertEqual(len(self.server.requests), 3, 'each write sent once, never repeated')

    def test_a_read_without_a_complete_response_is_retried(self):
        with mock.patch('todo_manager.notion.client.time.sleep'):
            for path in ('/drop', '/truncated', '/html'):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path, fail=1), {'object': 'ok'})
            with self.assertRaises(NotionError) as caught:
                self.client.get('/drop')
        self.assertNotIsInstance(caught.exception, NotionUncertain)
        self.assertEqual(sum(1 for r in self.server.requests if r[1] == '/drop'), 2 + self.client.max_retries + 1)
