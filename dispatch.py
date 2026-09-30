"""Durable receipt, atomic local state/response preparation, per-user delivery.

Only one task per user enters the pool. Queued answers never occupy threads
waiting on a same-user lock. Network I/O runs outside SQLite transactions.
"""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from background import cleanup, latency


class ResponsePlan:
    def __init__(self, platform):
        self.platform = platform
        self.calls = []

    def __getattr__(self, method):
        if method not in {'send', 'edit', 'answer_callback', 'defer_search', 'defer_broadcast'}:
            raise AttributeError(method)
        def record(*args, **kwargs):
            self.calls.append((method, args, kwargs))
        return record


class UpdateDispatcher:
    def __init__(self, transport, engine, admin, config, transports, handle):
        self.transport, self.engine, self.admin = transport, engine, admin
        self.config, self.transports, self.handle = config, transports, handle
        self.store = engine.store
        self.wake = threading.Event()
        self.closing = False
        self.pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix=transport.platform+'-user')
        self.thread = threading.Thread(target=self._schedule, name=transport.platform+'-dispatch')
        self.thread.start()

    @staticmethod
    def user_id(update):
        cb = update.get('callback_query') or {}
        message = update.get('message') or cb.get('message') or {}
        sender = (update.get('message') or {}).get('from') or cb.get('from') or {}
        return str(sender.get('id') or message.get('chat', {}).get('id') or '')

    def _schedule(self):
        active, failed = {}, {}
        while True:
            for user, future in list(active.items()):
                if future.done():
                    del active[user]
                    try:
                        future.result()
                        failed.pop(user, None)
                    except Exception:
                        logging.exception('Update processing failed (%s user=%s)', self.transport.platform, user)
                        failed[user] = time.monotonic() + 3
            users = self.store.db.execute(
                'SELECT user_id FROM update_queue WHERE platform=? GROUP BY user_id ORDER BY min(id)',
                (self.transport.platform,)).fetchall()
            for row in users:
                user = row['user_id']
                if user in active or (user in failed and (self.closing or failed[user] > time.monotonic())):
                    continue
                if len(active) >= 16:
                    break
                active[user] = self.pool.submit(self._process_user, user)
                active[user].add_done_callback(lambda _: self.wake.set())
            if self.closing and not active:
                break
            self.wake.wait(.05)
            self.wake.clear()

    def _process_user(self, user):
        # Yield after each update, so an active user cannot monopolize a worker.
        row = self.store._one('SELECT * FROM update_queue WHERE platform=? AND user_id=? ORDER BY id LIMIT 1',
                              (self.transport.platform,user))
        if row is None:
            return
        update = json.loads(row['payload'])
        context = dict(platform=self.transport.platform, user=user, update=row['update_key'])
        queued_ms = (time.time()-update.get('_received_at',time.time()))*1000
        if queued_ms > 200:
            logging.warning('latency section=update.queue duration_ms=%.1f platform=%s user=%s update=%s',
                            queued_ms,self.transport.platform,user,row['update_key'])
        with latency('update.response', **context):
            if row['responses'] is None:
                plan = ResponsePlan(self.transport.platform)
                with latency('update.local', **context):
                    with self.store.db:
                        self.handle(plan,update,self.engine,self.admin,self.config,self.transports)
                        self.store.db.execute('UPDATE update_queue SET responses=? WHERE id=?',
                                              (json.dumps(plan.calls,ensure_ascii=False),row['id']))
                        self.store.complete_update(self.transport.platform,row['update_key'])
                row = self.store._one('SELECT * FROM update_queue WHERE id=?',(row['id'],))
            for index, (method,args,kwargs) in enumerate(json.loads(row['responses'])):
                if index < row['response_index']:
                    continue
                with latency('transport.'+method, **context):
                    if method in {'defer_search','defer_broadcast'}:
                        # These paths have no candidate state mutations. Keep
                        # their network calls outside the local transaction.
                        self.handle(self.transport,args[0],self.engine,self.admin,self.config,self.transports)
                    elif method == 'answer_callback' and kwargs.get('inline') is None:
                        cleanup.submit(self._ack,*args,**kwargs)
                    else:
                        getattr(self.transport,method)(*args,**kwargs)
                with self.store.db:
                    self.store.db.execute('UPDATE update_queue SET response_index=? WHERE id=?',(index+1,row['id']))
            incoming = update.get('message') or {}
            agent_session = self.store._one(
                "SELECT state FROM agent_sessions WHERE platform=? AND user_id=?",
                (self.transport.platform, user),
            )
            preserving_client_form = bool(agent_session and str(agent_session['state']).startswith('client_'))
            if incoming.get('message_id') and not preserving_client_form:
                cleanup.submit(self.transport.delete,
                               str(incoming.get('chat',{}).get('id') or user),str(incoming['message_id']))
            with self.store.db:
                self.store.db.execute('DELETE FROM update_queue WHERE id=?',(row['id'],))

    def _ack(self, *args, **kwargs):
        try:
            self.transport.answer_callback(*args, **kwargs)
        except Exception:
            logging.warning('Callback acknowledgement failed (%s)', self.transport.platform, exc_info=True)

    def close(self):
        self.closing = True
        self.wake.set()
        self.thread.join()
        self.pool.shutdown(wait=True)
