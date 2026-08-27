import asyncio
import tempfile
import unittest
from pathlib import Path

from core.agent_runtime import SessionRuntime
from core.goal import GoalRuntime
from core.runtime import ArtifactStore, RuntimeStore, TaskEnvelope, TaskResult, TaskRuntime, TaskStatus
from core.runtime.quality_gate import QualityGate

class RuntimeMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.tmp.name) / 'runtime.db')

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_gateway_starts_and_stops_with_session_runtime(self):
        from gateway.server import GatewayServer
        cfg = {
            'host': '127.0.0.1', 'port': 0,
            'channels': {'debug': {'enabled': False}, 'webui': {'enabled': False}},
            'scheduler': {'enabled': False}, 'heartbeat': {'enabled': False},
            'sessions': {'max_sessions': 2, 'idle_timeout_minutes': 1, 'persist': False, 'worker_pool_size': 1, 'soft_timeout_seconds': 1, 'hard_timeout_seconds': 2},
            'agent': {'max_steps': 1, 'quiet': True},
            'runtime_store': {'path': str(Path(self.tmp.name) / 'gateway.db'), 'wal': False, 'busy_timeout_ms': 1000},
            'task_runtime': {'enabled': True, 'max_global_concurrency': 1, 'max_attempts': 1, 'cancel_grace_seconds': 0, 'zombie_max_seconds': 1},
            'artifacts': {'root': str(Path(self.tmp.name) / 'gateway-artifacts'), 'max_file_bytes': 1024},
            'retention': {'enabled': True, 'interval_seconds': 60},
        }
        server = GatewayServer(cfg)
        await server.start()
        self.assertTrue(server.dispatcher.task_runtime_enabled)
        await server.stop()

    async def test_webui_sse_receives_runtime_projection(self):
        from aiohttp import ClientSession
        from gateway.server import GatewayServer
        cfg = {'host':'127.0.0.1','port':0, 'channels':{'debug':{'enabled':False},'webui':{'enabled':True}}, 'webui':{'allow_non_loopback':False}, 'scheduler':{'enabled':False}, 'heartbeat':{'enabled':False}, 'sessions':{'max_sessions':2,'idle_timeout_minutes':1,'persist':False,'worker_pool_size':1,'soft_timeout_seconds':1,'hard_timeout_seconds':2}, 'agent':{'max_steps':1,'quiet':True}, 'runtime_store':{'path':str(Path(self.tmp.name)/'sse.db'),'wal':False,'busy_timeout_ms':1000}, 'task_runtime':{'enabled':True,'max_global_concurrency':1,'max_attempts':1,'cancel_grace_seconds':0,'zombie_max_seconds':1}, 'artifacts':{'root':str(Path(self.tmp.name)/'sse-artifacts'),'max_file_bytes':1024}, 'retention':{'enabled':True,'interval_seconds':60}}
        server = GatewayServer(cfg)
        await server.start()
        port = server._site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as client:
                response = await client.get(f'http://127.0.0.1:{port}/api/events')
                await response.content.readuntil(b'\n\n')
                server.webui.bus.publish('goal.changed', {'goal_id': 'g1'})
                chunk = await asyncio.wait_for(response.content.readuntil(b'\n\n'), timeout=2)
                self.assertIn(b'goal.changed', chunk)
                response.close()
        finally:
            await server.stop()

    async def test_webui_sse_live_streams_are_isolated_between_sessions(self):
        """Two concurrent scoped subscribers must never consume each other's event."""
        from aiohttp import ClientSession
        from gateway.server import GatewayServer
        cfg = {'host':'127.0.0.1','port':0, 'channels':{'debug':{'enabled':False},'webui':{'enabled':True}}, 'webui':{'allow_non_loopback':False}, 'scheduler':{'enabled':False}, 'heartbeat':{'enabled':False}, 'sessions':{'max_sessions':2,'idle_timeout_minutes':1,'persist':False,'worker_pool_size':1,'soft_timeout_seconds':1,'hard_timeout_seconds':2}, 'agent':{'max_steps':1,'quiet':True}, 'runtime_store':{'path':str(Path(self.tmp.name)/'sse-scope.db'),'wal':False,'busy_timeout_ms':1000}, 'task_runtime':{'enabled':True,'max_global_concurrency':1,'max_attempts':1,'cancel_grace_seconds':0,'zombie_max_seconds':1}, 'artifacts':{'root':str(Path(self.tmp.name)/'sse-scope-artifacts'),'max_file_bytes':1024}, 'retention':{'enabled':True,'interval_seconds':60}}
        server = GatewayServer(cfg)
        await server.start()
        port = server._site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as client:
                first = await client.get(f'http://127.0.0.1:{port}/api/events?session_key=s1')
                second = await client.get(f'http://127.0.0.1:{port}/api/events?session_key=s2')
                await first.content.readuntil(b'\n\n')
                await second.content.readuntil(b'\n\n')
                server.webui.bus.publish('plan.changed', {
                    'session_key': 's1', 'plan': {'plan_id': 'p1'}})
                server.webui.bus.publish('goal.changed', {
                    'session_key': 's2', 'goal': {'goal_id': 'g2'}})
                first_chunk, second_chunk = await asyncio.gather(
                    asyncio.wait_for(first.content.readuntil(b'\n\n'), timeout=2),
                    asyncio.wait_for(second.content.readuntil(b'\n\n'), timeout=2),
                )
                self.assertIn(b'"session_key": "s1"', first_chunk)
                self.assertNotIn(b'"session_key": "s2"', first_chunk)
                self.assertIn(b'"session_key": "s2"', second_chunk)
                self.assertNotIn(b'"session_key": "s1"', second_chunk)
                first.close()
                second.close()
        finally:
            await server.stop()

    async def test_webui_sse_scoped_stream_drops_missing_scope_events(self):
        """A scoped subscriber must not receive an unscoped runtime event."""
        from aiohttp import ClientSession
        from gateway.server import GatewayServer
        cfg = {'host':'127.0.0.1','port':0, 'channels':{'debug':{'enabled':False},'webui':{'enabled':True}}, 'webui':{'allow_non_loopback':False}, 'scheduler':{'enabled':False}, 'heartbeat':{'enabled':False}, 'sessions':{'max_sessions':2,'idle_timeout_minutes':1,'persist':False,'worker_pool_size':1,'soft_timeout_seconds':1,'hard_timeout_seconds':2}, 'agent':{'max_steps':1,'quiet':True}, 'runtime_store':{'path':str(Path(self.tmp.name)/'sse-missing-scope.db'),'wal':False,'busy_timeout_ms':1000}, 'task_runtime':{'enabled':True,'max_global_concurrency':1,'max_attempts':1,'cancel_grace_seconds':0,'zombie_max_seconds':1}, 'artifacts':{'root':str(Path(self.tmp.name)/'sse-missing-scope-artifacts'),'max_file_bytes':1024}, 'retention':{'enabled':True,'interval_seconds':60}}
        server = GatewayServer(cfg)
        await server.start()
        port = server._site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as client:
                response = await client.get(f'http://127.0.0.1:{port}/api/events?session_key=s1')
                await response.content.readuntil(b'\n\n')
                server.webui.bus.publish('plan.changed', {'plan': {'plan_id': 'unscoped'}})
                server.webui.bus.publish('plan.changed', {
                    'session_key': 's1', 'plan': {'plan_id': 'scoped'}})
                chunk = await asyncio.wait_for(response.content.readuntil(b'\n\n'), timeout=2)
                self.assertIn(b'"plan_id": "scoped"', chunk)
                self.assertNotIn(b'unscoped', chunk)
                response.close()
        finally:
            await server.stop()

    async def test_feishu_proactive_replay_failure_is_reported(self):
        from gateway.channels.base import InboundMessage
        from gateway.channels.feishu_channel import FeishuChannel
        channel = object.__new__(FeishuChannel); channel._client = object(); channel.send_to_chat = lambda chat, text: False
        message = InboundMessage(channel='feishu', session_key='f', user_id='u', user_name='U', text='', message_id='m', metadata={'route_chat_id': 'chat_1'})
        self.assertFalse(channel._do_reply(message, 'summary'))

    async def test_external_channel_replay_route_mocks(self):
        from gateway.channels.base import InboundMessage
        from gateway.channels.feishu_channel import FeishuChannel
        from gateway.channels.weixin_channel import WeixinChannel
        feishu = object.__new__(FeishuChannel); feishu._client = object(); sent = []
        feishu.send_to_chat = lambda chat_id, text: (sent.append((chat_id, text)), True)[1]
        message = InboundMessage(channel='feishu', session_key='f', user_id='u', user_name='U', text='', message_id='m', metadata={'route_chat_id': 'chat_1'})
        self.assertTrue(feishu._do_reply(message, 'summary'))
        self.assertEqual(sent, [('chat_1', 'summary')])
        weixin = object.__new__(WeixinChannel); weixin._bot = type('Bot', (), {'send_text': lambda self, user, text: sent.append((user, text))})()
        weixin.reply_format = 'text'
        self.assertTrue(weixin._do_send(InboundMessage(channel='weixin', session_key='w', user_id='user_1', user_name='U', text='', message_id='m'), 'summary'))
        self.assertIn(('user_1', 'summary'), sent)

    async def test_webui_goal_and_subagent_rest_endpoints(self):
        from aiohttp import ClientSession
        from gateway.server import GatewayServer
        cfg = {'host':'127.0.0.1','port':0, 'channels':{'debug':{'enabled':False},'webui':{'enabled':True}}, 'webui':{'allow_non_loopback':False}, 'scheduler':{'enabled':False}, 'heartbeat':{'enabled':False}, 'sessions':{'max_sessions':2,'idle_timeout_minutes':1,'persist':False,'worker_pool_size':1,'soft_timeout_seconds':1,'hard_timeout_seconds':2}, 'agent':{'max_steps':1,'quiet':True}, 'runtime_store':{'path':str(Path(self.tmp.name)/'rest.db'),'wal':False,'busy_timeout_ms':1000}, 'task_runtime':{'enabled':True,'max_global_concurrency':1,'max_attempts':1,'cancel_grace_seconds':0,'zombie_max_seconds':1}, 'artifacts':{'root':str(Path(self.tmp.name)/'rest-artifacts'),'max_file_bytes':1024}, 'retention':{'enabled':True,'interval_seconds':60}}
        server = GatewayServer(cfg); await server.start()
        port = server._site._server.sockets[0].getsockname()[1]
        try:
            session_id = 'sess_api'
            server.runtime_store.upsert_session(session_id, 'webui:api', channel='webui')
            goal = server.webui.goal_runtime.create(session_id, 'archive endpoint')
            goal = server.webui.goal_runtime.complete(goal.goal_id)
            async with ClientSession() as client:
                response = await client.post(f'http://127.0.0.1:{port}/api/goals/{goal.goal_id}/archive', json={})
                self.assertEqual(response.status, 200)
                # 清除 = 业务表记录删除；会话与记忆中的审计历史不受影响
                self.assertIsNone(server.webui.goal_runtime.get(goal.goal_id))
                children = await client.get(f'http://127.0.0.1:{port}/api/sessions/webui%3Aapi/children')
                self.assertEqual(children.status, 200)
                self.assertEqual((await children.json())['children'], [])
        finally:
            await server.stop()

    async def test_workspace_switch_releases_loaded_agent_without_general_eviction(self):
        from gateway.webui.api_workspace import _rebuild_on_next_message
        from gateway.webui.workspace_models import WorkspaceSession
        class Service:
            def __init__(self): self.marked = False
            def mark_stale(self, workspace_id, session_id): self.marked = True
        class Manager:
            def __init__(self, entry): self._sessions = {entry.session_key: entry}; self.evict_called = False
            async def evict(self, *args, **kwargs): self.evict_called = True
        class Entry:
            session_key = 'workspace:ws_switch:switch_session'; agent = object()
        class Module: pass
        entry = Entry(); module = Module(); module.session_mgr = Manager(entry)
        service = Service()
        import gateway.webui.api_workspace as api_workspace
        previous = api_workspace._runtime_service_cache.get(module)
        api_workspace._runtime_service_cache[module] = service
        try:
            await _rebuild_on_next_message(module, WorkspaceSession('switch_session', 'ws_switch'))
        finally:
            if previous is None: api_workspace._runtime_service_cache.pop(module, None)
            else: api_workspace._runtime_service_cache[module] = previous
        self.assertTrue(service.marked)
        self.assertIsNone(entry.agent)
        self.assertFalse(module.session_mgr.evict_called)

    async def test_workspace_session_control_switches_persist_and_bump_version(self):
        from gateway.webui.workspace_models import Workspace
        from gateway.webui.workspace_store import WorkspaceDatabase, WorkspaceSessionStore, WorkspaceStore
        db = WorkspaceDatabase(runtime_store=self.store)
        workspace = Workspace('ws_controls', 'controls', self.tmp.name)
        WorkspaceStore(db).create(workspace)
        sessions = WorkspaceSessionStore(db)
        session = sessions.create(workspace.workspace_id, {'agent_profile_id': 'agent_coder'})
        updated = sessions.update_runtime_overrides(
            workspace.workspace_id, session.session_id, model='model-x',
            reasoning_level='high', permission_mode='allow')
        self.assertEqual(updated.model, 'model-x')
        self.assertEqual(updated.reasoning_level, 'high')
        self.assertEqual(updated.permission_mode, 'allow')
        self.assertEqual(updated.client_config_version, session.client_config_version + 1)

    async def test_agent_profile_system_templates_include_frontend_product_and_test_workflows(self):
        from gateway.webui.workspace_store import AgentProfileStore, WorkspaceDatabase

        profile_store = AgentProfileStore(WorkspaceDatabase(runtime_store=self.store))
        inserted = profile_store.seed_system_profiles()
        self.assertEqual(inserted, 5)

        profiles = {profile.profile_id: profile for profile in profile_store.list()}
        self.assertEqual(
            set(profiles),
            {
                'agent_coder',
                'agent_frontend',
                'agent_product',
                'agent_tester',
                'agent_reviewer',
            },
        )
        frontend = profiles['agent_frontend']
        self.assertIn('react-patterns', frontend.skills)
        self.assertIn('design-review', frontend.skills)
        self.assertIn('vue-best-practices', frontend.skills)
        self.assertIn('可访问', frontend.system_prompt)
        self.assertIn('响应式', frontend.system_prompt)

        product = profiles['agent_product']
        self.assertIn('grill-me', product.skills)
        self.assertIn('project-docs', product.skills)
        self.assertIn('Given/When/Then', product.system_prompt)
        self.assertIn('目标与非目标', product.system_prompt)

        tester = profiles['agent_tester']
        self.assertIn('diagnosing-bugs', tester.skills)
        self.assertIn('vue-testing-best-practices', tester.skills)
        self.assertIn('覆盖矩阵', tester.system_prompt)
        self.assertIn('发布建议', tester.system_prompt)

    async def test_agent_profile_system_seed_preserves_existing_admin_edits(self):
        from gateway.webui.workspace_store import AgentProfileStore, WorkspaceDatabase

        profile_store = AgentProfileStore(WorkspaceDatabase(runtime_store=self.store))
        self.assertEqual(profile_store.seed_system_profiles(), 5)
        with self.store.connection() as connection:
            connection.execute(
                'UPDATE agent_profiles SET system_prompt = ? WHERE profile_id = ?',
                ('管理员自定义提示词', 'agent_product'),
            )

        self.assertEqual(profile_store.seed_system_profiles(), 0)
        self.assertEqual(
            profile_store.get('agent_product').system_prompt,
            '管理员自定义提示词',
        )

    async def test_v11_normalizes_retired_plan_modes_for_workspace_and_profiles(self):
        from gateway.webui.workspace_store import AgentProfileStore, WorkspaceDatabase, WorkspaceStore
        db = WorkspaceDatabase(runtime_store=self.store)
        with self.store.connection() as connection:
            now = '2026-01-01T00:00:00+00:00'
            connection.execute(
                "INSERT INTO agent_profiles (profile_id,name,chat_mode,created_at,updated_at) VALUES (?,?,?,?,?)",
                ('agent_legacy_plan', 'legacy plan profile', 'plan', now, now))
            connection.execute(
                "INSERT INTO workspaces (workspace_id,name,project_path,working_directory,chat_mode,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                ('ws_legacy_plan', 'legacy plan workspace', self.tmp.name, self.tmp.name, 'plan', now, now))
        # The startup normalization is deliberately idempotent, so it also heals
        # stale rows inserted after a database has already reached schema v11.
        with self.store.connection() as connection:
            RuntimeStore._normalize_retired_profile_chat_modes(connection)
        self.assertEqual(AgentProfileStore(db).get('agent_legacy_plan').chat_mode, 'chat')
        self.assertEqual(WorkspaceStore(db).get('ws_legacy_plan').chat_mode, 'chat')

    async def test_structured_capability_tools_are_llm_visible_for_root_only(self):
        from gateway.webui.runtime_tools import register_structured_capability_tools
        class Agent:
            def __init__(self):
                from tools.registry import ToolRegistry
                self.tool_registry = ToolRegistry(); self.rebuilt = 0
            def _rebuild_system_prompt(self): self.rebuilt += 1
        class Entry:
            session_key = 'webui:test'; agent = None
        class Module: pass
        agent = Agent(); entry = Entry(); entry.agent = agent
        register_structured_capability_tools(agent, Module(), entry)
        self.assertEqual(set(('create_plan', 'create_goal', 'create_subagent')),
                         {name for name in agent.tool_registry.list_tool_names() if name.startswith('create_')})
        self.assertEqual(agent.rebuilt, 1)

    async def test_structured_plan_and_goal_events_carry_the_same_workspace_scope(self):
        """Model-callable Plan/Goal creation must publish an identical full scope."""
        from gateway.webui.runtime_tools import CreateGoalTool, CreatePlanTool
        published = []
        started = []
        class Bus:
            _loop = asyncio.get_running_loop()
            def publish(self, event, payload): published.append((event, payload))
        class Plan:
            plan_id = 'p1'; status = type('Status', (), {'value': 'approved'})()
            def to_dict(self): return {'plan_id': self.plan_id, 'status': 'approved'}
        class PlanManager:
            def approve(self, plan_id, actor): return Plan()
        class Glue:
            plan_manager = PlanManager()
            def plan_preview_sync(self, agent, objective): return {'plan': {'objective': objective}}
            def create_plan(self, session_key, objective, preview): return Plan()
        class Goal:
            goal_id = 'g1'
            def to_dict(self): return {'goal_id': self.goal_id, 'status': 'active'}
        class GoalRuntime:
            def create(self, session_id, objective, max_rounds=20): return Goal()
        class RuntimeStore:
            def upsert_session(self, *args, **kwargs): return None
        class Dispatcher:
            @staticmethod
            def _runtime_session_id(session_key): return 'session-1'
        class GoalDriver:
            def trigger(self, goal_id): started.append(('goal', goal_id))
        class PlanRuntime:
            def start(self, plan_id): started.append(('plan', plan_id))
        class Module:
            bus = Bus(); glue = Glue(); goal_runtime = GoalRuntime()
            runtime_store = RuntimeStore(); dispatcher = Dispatcher()
            goal_driver = GoalDriver(); plan_runtime = PlanRuntime()
        class Entry: session_key = 'workspace:w1:s1'
        class Agent: pass
        module = Module()
        CreatePlanTool(module, Entry(), Agent()).execute('plan objective')
        CreateGoalTool(module, Entry(), Agent()).execute('goal objective')
        await asyncio.sleep(0)
        scoped = [payload for event, payload in published
                  if event in {'plan.changed', 'goal.changed'}]
        self.assertEqual(len(scoped), 2)
        for payload in scoped:
            self.assertEqual(payload['session_key'], 'workspace:w1:s1')
            self.assertEqual(payload['workspace_id'], 'w1')
            self.assertEqual(payload['workspace_session_id'], 's1')

    async def test_parallel_safe_tool_batch_preserves_source_order(self):
        from core.agent_runtime.tools import PreparedToolCall, ToolBatchExecutor
        class Tool: parallel_safe = True
        class Registry:
            def get_tool(self, name): return Tool()
        class Agent:
            tool_registry = Registry(); _config = {'agent_runtime': {'max_parallel_tools': 2}}
            def _execute_native_tool_call(self, call_id, provider, tool, arguments, raw):
                import time; time.sleep(arguments['delay']); return (provider, False)
        calls = [PreparedToolCall('first','first','first',{'delay':0.03},'{}',1), PreparedToolCall('second','second','second',{'delay':0.0},'{}',2)]
        results = ToolBatchExecutor(Agent()).execute(calls)
        self.assertEqual([result[0].provider_name for result in results], ['first', 'second'])
        self.assertEqual([result[1] for result in results], ['first', 'second'])

    async def test_agent_context_and_batch_preserve_source_order(self):
        from core.agent_runtime.context import AgentContext
        from core.agent_runtime.tools import ToolBatchExecutor
        class Call:
            def __init__(self, name, order): self.name, self.order, self.call_id, self.arguments, self.raw_arguments = name, order, f'id{order}', {}, '{}'
        class Registry:
            def get_tool(self, name): return object()
        class Agent:
            tool_registry = Registry()
            def _execute_native_tool_call(self, call_id, provider, tool, arguments, raw): return (tool, False)
        source = [{'role': 'user', 'content': 'x'}]
        view = AgentContext(source).llm_messages()
        view[0]['content'] = 'changed'
        self.assertEqual(source[0]['content'], 'x')
        batch = ToolBatchExecutor(Agent())
        prepared = batch.prepare([Call('b', 2), Call('a', 1)], {})
        self.assertEqual([call.provider_name for call in prepared], ['a', 'b'])
        self.assertEqual([result[1] for result in batch.execute(prepared)], ['a', 'b'])

    async def test_session_runtime_emits_ordered_lifecycle(self):
        async def unused(envelope, token):
            raise AssertionError('SessionRuntime must replace the executor')
        async def execute(envelope, token, emit):
            emit('message/start', {'message_id': 'message_1'})
            emit('message/patch', {'text': 'ok'})
            emit('message/end', {'message_id': 'message_1'})
            return TaskResult(task_id=envelope.task_id, status=TaskStatus.COMPLETED, visible_text='ok')
        runtime = SessionRuntime(TaskRuntime(self.store, unused), execute)
        await runtime.start()
        events = runtime.subscribe('session_1')
        task_id = await runtime.submit(session_id='session_1', session_key='key_1', prompt='hello', source='subagent')
        result = await runtime.wait(task_id, timeout=2)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        types = []
        while not events.empty(): types.append(events.get_nowait().type)
        self.assertEqual(types[0], 'run.started')
        self.assertIn('message/patch', types)
        self.assertEqual(types[-1], 'run.ended')
        await runtime.stop()

    async def test_goal_versioned_lifecycle(self):
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_1', 'finish the migration')
        paused = runtime.pause(goal.goal_id, expected_version=goal.version)
        self.assertEqual(paused.status.value, 'paused')
        resumed = runtime.resume(goal.goal_id, expected_version=paused.version)
        done = runtime.complete(resumed.goal_id, expected_version=resumed.version)
        self.assertEqual(done.status.value, 'completed')
        with self.assertRaises(RuntimeError):
            runtime.pause(done.goal_id, expected_version=1)


    async def test_event_bus_concurrent_publish_preserves_unique_monotonic_ids(self):
        from concurrent.futures import ThreadPoolExecutor
        from gateway.webui.events import EventBus

        bus = EventBus(backlog_size=256)
        bus.bind_loop(asyncio.get_running_loop())
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(bus.publish, 'thread.event', {'index': index})
                for index in range(200)
            ]
            for future in futures:
                future.result()

        event_ids = [event['event_id'] for event in bus.replay()]
        self.assertEqual(event_ids, list(range(1, 201)))
        self.assertEqual(bus.last_event_id(), 200)

    async def test_event_bus_replay_scopes_session(self):
        from gateway.webui.events import EventBus
        bus = EventBus()
        bus._loop = asyncio.get_running_loop()
        bus.publish('chat.started', {'session_key': 's1'})
        bus.publish('chat.started', {'session_key': 's2'})
        self.assertEqual([e['data']['session_key'] for e in bus.replay(session_key='s1')], ['s1'])

    async def test_event_bus_live_scope_match_uses_replay_semantics(self):
        from gateway.webui.events import EventBus
        scoped = {'data': {'session_key': 's1', 'workspace_id': 'w1',
                           'workspace_session_id': 'ws1'}}
        wrong = {'data': {'session_key': 's2', 'workspace_id': 'w2',
                          'workspace_session_id': 'ws2'}}
        partial = {'data': {'session_key': 's1'}}
        unscoped = {'data': {}}
        scope = {'session_key': 's1', 'workspace_id': 'w1',
                 'workspace_session_id': 'ws1'}
        self.assertTrue(EventBus.matches_scope(scoped, **scope))
        self.assertFalse(EventBus.matches_scope(wrong, **scope))
        self.assertFalse(EventBus.matches_scope(partial, **scope))
        self.assertFalse(EventBus.matches_scope(unscoped, **scope))
        self.assertTrue(EventBus.matches_scope(unscoped))

    async def test_event_bus_drop_oldest_broadcasts_version_gap(self):
        """背压丢最旧 → 向订阅者广播 version_gap（设计方案 18.4）。"""
        from gateway.webui.events import EventBus, _QUEUE_MAX
        bus = EventBus()
        bus._loop = asyncio.get_running_loop()
        sub_id, q = bus.subscribe()
        try:
            # 直接灌满订阅队列（绕过 call_soon 时序），再发布触发丢最旧
            for i in range(_QUEUE_MAX):
                q.put_nowait({
                    'type': 'turn.status',
                    'data': {'conversation_id': f'conv-{i}', 'session_key': 's1',
                             'scope': 'turn', 'version': i + 1, 'turn_id': f't{i}'},
                    'at': 0.0, 'event_id': i + 1,
                })
            bus.publish('queue.updated', {
                'conversation_id': 'conv-overflow', 'session_key': 's1',
                'scope': 'session', 'version': 999, 'data': {}})
            for _ in range(10):
                await asyncio.sleep(0.01)
            events = []
            while not q.empty():
                evt = q.get_nowait()
                if evt is not None:
                    events.append(evt)
            gaps = [e for e in events if e['type'] == 'version_gap']
            self.assertGreaterEqual(len(gaps), 1)
            gap = gaps[0]
            self.assertEqual(gap['data']['scope'], 'session')
            self.assertEqual(gap['data']['session_key'], 's1')
            self.assertTrue(
                isinstance(gap['data']['conversation_id'], str)
                and gap['data']['conversation_id'])
        finally:
            bus.unsubscribe(sub_id)

    async def test_sse_replay_raises_watermark_no_duplicate_delivery(self):
        """replay 结束后水位抬升 → 实时循环不再重复投递同 id 事件。"""
        from gateway.webui.events import EventBus, SSEHandler
        bus = EventBus()
        bus._loop = asyncio.get_running_loop()
        handler = SSEHandler(bus)
        scope = {'session_key': 'webui:dup', 'workspace_id': '',
                 'workspace_session_id': ''}
        # 订阅前先发 3 条历史事件
        for i in range(1, 4):
            bus.publish('turn.status', {'session_key': 'webui:dup',
                                        'conversation_id': 'conv-dup',
                                        'seq': i})
        sub_id, q = bus.subscribe(**scope)
        watermark = bus.watermark(sub_id)
        try:
            # 订阅与 replay 之间又发布 2 条：旧实现下它们会同时出现在
            # replay 结果与实时队列里被投递两遍（双投窗口）。
            bus.publish('turn.status', {'session_key': 'webui:dup',
                                        'conversation_id': 'conv-dup',
                                        'seq': 4})
            bus.publish('turn.status', {'session_key': 'webui:dup',
                                        'conversation_id': 'conv-dup',
                                        'seq': 5})
            replay_events, gap_frame, raised = handler._replay_plan(
                scope, last_id=2, since=0.0, watermark=watermark)
            self.assertIsNone(gap_frame)  # 无缺口：min 可用 id(=1) <= last+1
            self.assertEqual([e['event_id'] for e in replay_events], [3, 4, 5])
            self.assertGreaterEqual(raised, 5)
            # 模拟实时循环的去重过滤（handle() 同款条件）
            delivered = [e['event_id'] for e in replay_events]
            while not q.empty():
                evt = q.get_nowait()
                if evt is None:
                    continue
                if evt.get('event_id', 0) <= raised:
                    continue  # replay 阶段已投递 → 去重
                delivered.append(evt['event_id'])
            self.assertEqual(sorted(delivered), [3, 4, 5])
        finally:
            bus.unsubscribe(sub_id)

    async def test_sse_backlog_gap_emits_version_gap_frame(self):
        """Last-Event-ID 落后被淘汰的区间 → 补发 backlog_expired 缺口帧。"""
        from gateway.webui.events import EventBus, SSEHandler
        bus = EventBus(backlog_size=3)
        bus._loop = asyncio.get_running_loop()
        handler = SSEHandler(bus)
        scope = {'session_key': 'webui:gap', 'workspace_id': '',
                 'workspace_session_id': ''}
        # 灌 6 条使 backlog 容量淘汰只剩 id 4..6；last_event_id=1 → 缺口
        for i in range(1, 7):
            bus.publish('turn.status', {'session_key': 'webui:gap',
                                        'conversation_id': 'conv-gap',
                                        'seq': i})
        self.assertEqual(bus.min_replayable_event_id(), 4)
        replayed = bus.replay(after_event_id=1, **scope)
        self.assertEqual([e['event_id'] for e in replayed], [4, 5, 6])
        frame = handler._backlog_gap_frame(scope, last_id=1, replayed=replayed)
        self.assertIsNotNone(frame)
        self.assertEqual(frame['type'], 'version_gap')
        self.assertEqual(frame['data']['reason'], 'backlog_expired')
        self.assertEqual(frame['data']['scope'], 'session')
        self.assertEqual(frame['data']['session_key'], 'webui:gap')
        self.assertEqual(frame['data']['conversation_id'], 'conv-gap')
        self.assertEqual(frame['event_id'], 6)

    async def test_sse_backlog_gap_skipped_without_conversation_id(self):
        """推导不出 conversation_id → 不合成帧，仅记 info 日志。

        前端 useConversation.isGatewayEvent 与 store.applyEvent 都会直接
        丢弃缺 conversation_id 的事件（帧无效），此时只留日志。"""
        from gateway.webui.events import EventBus, SSEHandler
        bus = EventBus(backlog_size=3)
        bus._loop = asyncio.get_running_loop()
        handler = SSEHandler(bus)
        scope = {'session_key': 'webui:nocid', 'workspace_id': '',
                 'workspace_session_id': ''}
        for i in range(1, 7):
            bus.publish('hook.event', {'session_key': 'webui:nocid',
                                       'seq': i})
        replayed = bus.replay(after_event_id=1, **scope)
        with self.assertLogs('jk_agent.gateway', level='INFO') as logs:
            frame = handler._backlog_gap_frame(scope, last_id=1,
                                               replayed=replayed)
        self.assertIsNone(frame)
        self.assertTrue(any('跳过合成' in line for line in logs.output))

    async def test_sse_no_gap_when_backlog_covers_last_event_id(self):
        """backlog 仍覆盖 last_event_id+1 → 不误报缺口。"""
        from gateway.webui.events import EventBus, SSEHandler
        bus = EventBus()
        bus._loop = asyncio.get_running_loop()
        handler = SSEHandler(bus)
        scope = {'session_key': 's', 'workspace_id': '',
                 'workspace_session_id': ''}
        bus.publish('turn.status', {'session_key': 's',
                                    'conversation_id': 'c', 'seq': 1})
        bus.publish('turn.status', {'session_key': 's',
                                    'conversation_id': 'c', 'seq': 2})
        frame = handler._backlog_gap_frame(
            scope, last_id=1,
            replayed=bus.replay(after_event_id=1, **scope))
        self.assertIsNone(frame)

    async def test_runtime_event_scope_includes_workspace_dimensions(self):
        from gateway.webui.api_chat import _session_event_scope
        self.assertEqual(_session_event_scope('webui:default'), {
            'session_key': 'webui:default',
        })
        self.assertEqual(_session_event_scope('workspace:w1:s1'), {
            'session_key': 'workspace:w1:s1',
            'workspace_id': 'w1',
            'workspace_session_id': 's1',
        })

    async def test_memory_user_call_index_keeps_long_query_prefix(self):
        from memory.manager import MemoryManager
        memory = MemoryManager(str(Path(self.tmp.name) / 'memory'))
        query = 'x' * 800
        memory.save_conversation(query, [{'role': 'user', 'content': query}], 'memory-session')
        index = memory._load_index()
        self.assertEqual(len(index[0]['user_call']), 800)

    async def test_emergency_truncate_invalidates_anchor(self):
        from agent import Agent
        from core.message_store import MessageStore
        class Dummy: context_length = 100
        agent = object.__new__(Agent)
        agent.store = MessageStore(max_tokens=100)
        agent.messages = agent.store.messages
        agent.max_history_tokens = 100
        agent.store.set_anchor({'input_tokens': 120, 'output_tokens': 0})
        agent.messages.extend([{'role':'system','content':'s'}, {'role':'user','content':'a'*500}, {'role':'assistant','content':'b'*500}, {'role':'user','content':'c'*500}])
        agent._truncate_history()
        self.assertEqual(agent.store._anchor_total, 0)

    async def test_goal_activation_round_reservation_and_limit(self):
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_rounds', 'finish work', max_rounds=1)
        self.assertEqual(goal.activation.value, 'armed')
        reserved = runtime.reserve_round(goal.goal_id, 'round_task', expected_version=goal.version)
        self.assertEqual((reserved.rounds_started, reserved.current_task_id), (1, 'round_task'))
        finished = runtime.finish_round(goal.goal_id, 'round_task', summary='progress', expected_version=reserved.version)
        blocked = runtime.reserve_round(goal.goal_id, 'round_task_2', expected_version=finished.version)
        self.assertEqual(blocked.status.value, 'blocked')
        self.assertEqual(blocked.activation.value, 'disarmed')

    async def test_legacy_active_goal_defaults_to_disarmed(self):
        from core.goal import Goal
        raw = Goal.create(session_id='legacy', objective='legacy').to_dict()
        raw.pop('activation')
        loaded = Goal.from_dict(raw)
        self.assertEqual(loaded.activation.value, 'disarmed')

    async def test_goal_driver_defers_to_user_work(self):
        from core.goal import GoalRoundDriver
        self.store.upsert_session('session_priority', 'priority-key')
        goal = GoalRuntime(self.store).create('session_priority', 'continue', max_rounds=2)
        user = TaskEnvelope.create(session_id='session_priority', session_key='priority-key', source='user', prompt='human')
        self.store.create_task(user)
        submitted = []
        async def submit(**kwargs): submitted.append(kwargs); return kwargs['task_id']
        async def wait(task_id): return TaskResult(task_id=task_id, status=TaskStatus.CANCELLED)
        driver = GoalRoundDriver(GoalRuntime(self.store), self.store, submit=submit, wait=wait,
                                 session_key=lambda _sid: 'priority-key', idle_delay=0.01)
        driver.trigger(goal.goal_id)
        await asyncio.sleep(0.04)
        self.assertEqual(submitted, [])
        self.store.request_cancel(user.task_id, 'done')
        await asyncio.sleep(0.05)
        self.assertEqual(len(submitted), 1)
        await driver.stop()

    async def test_goal_driver_continues_after_completed_round(self):
        from core.goal import GoalRoundDriver
        self.store.upsert_session('session_goal_loop', 'goal-loop-key')
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_goal_loop', 'continue twice', max_rounds=2)
        submitted = []
        async def submit(**kwargs):
            submitted.append(kwargs['task_id'])
            return kwargs['task_id']
        async def wait(task_id):
            return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED,
                              visible_text='round complete', summary='round complete')
        driver = GoalRoundDriver(runtime, self.store, submit=submit, wait=wait,
                                 session_key=lambda _sid: 'goal-loop-key', idle_delay=0.001)
        driver.trigger(goal.goal_id)
        await asyncio.sleep(0.08)
        current = runtime.get(goal.goal_id)
        self.assertEqual(len(submitted), 2)
        self.assertEqual(current.rounds_started, 2)
        self.assertEqual(current.status.value, 'blocked')
        self.assertEqual(current.blocked_reason['type'], 'max_rounds')
        await driver.stop()

    async def test_goal_driver_ignores_interrupted_user_work(self):
        """A stale INTERRUPTED user task must not block Goal auto-continuation.

        ``_user_work_waiting`` used to treat INTERRUPTED as active user work, so
        a Gateway restart that left a user task in INTERRUPTED state (recovery
        keeps ``requeue=False`` for user tasks) would make the GoalRoundDriver
        wait forever and never submit its first round.
        """
        from core.goal import GoalRoundDriver
        self.store.upsert_session('session_int', 'int-key')
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_int', 'continue despite stale task', max_rounds=1)
        stale = TaskEnvelope.create(session_id='session_int', session_key='int-key',
                                    source='user', prompt='stale user work')
        self.store.create_task(stale)
        self.store.transition_task(stale.task_id, TaskStatus.LEASED, lease_owner='w-0')
        self.store.transition_task(stale.task_id, TaskStatus.RUNNING)
        self.store.transition_task(stale.task_id, TaskStatus.INTERRUPTED)

        submitted = []
        async def submit(**kwargs):
            submitted.append(kwargs['task_id'])
            return kwargs['task_id']
        async def wait(task_id):
            return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED,
                              visible_text='ok', summary='ok')
        driver = GoalRoundDriver(runtime, self.store, submit=submit, wait=wait,
                                 session_key=lambda _sid: 'int-key', idle_delay=0.01)
        driver.trigger(goal.goal_id)
        await asyncio.sleep(0.06)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(runtime.get(goal.goal_id).rounds_started, 1)
        await driver.stop()

    async def test_agent_runtime_scope_is_cleared_after_execution(self):
        """A completed agent run must reset the per-task runtime scope.

        ``Dispatcher._execute_agent`` used to reference an undefined
        ``prior_blocklist`` in its done-callback, so ``agent._runtime_task_source``
        and friends were never cleared after a run, leaking the previous task's
        source/goal into the next turn.
        """
        import gateway.dispatcher as disp_mod
        from gateway.session import SessionManager
        from gateway.channels.base import InboundMessage

        class FakeChannel:
            name = "webui"
            handles_chunking = True
            def __init__(self): self.replies = []
            async def start(self): pass
            async def stop(self): pass
            async def send_reply(self, msg, text): self.replies.append(text)
            async def send_progress(self, msg, text): pass

        class FakeToolRegistry:
            _skill_tool_names = set()
            def get_tool(self, name): return None
            def list_tool_names(self): return []

        class FakeStore:
            session_id = "fake-session"
            def save_session(self): pass

        class FakeLLM:
            model = "fake-model"

        class FakeAgent:
            def __init__(self):
                self.tool_registry = FakeToolRegistry()
                self.messages = []
                self.store = FakeStore()
                self.llm = FakeLLM()
            def run(self, user_input, verbose=True, images=None, event_sink=None):
                self.messages.append({"role": "assistant", "content": "ok"})
                return "ok"
            def request_stop(self): pass

        original = disp_mod.create_gateway_agent
        disp_mod.create_gateway_agent = lambda **kwargs: FakeAgent()
        try:
            session_mgr = SessionManager(max_sessions=5, idle_timeout_minutes=1,
                                         persist=False, worker_pool_size=2, agent_config={})
            dispatcher = disp_mod.Dispatcher(
                session_mgr=session_mgr,
                agent_config={"max_steps": 5, "permission_mode": "allow",
                              "auto_approve_plan": True, "soft_timeout_seconds": 5,
                              "hard_timeout_seconds": 10},
                runtime_store=self.store,
                task_runtime_config={"enabled": True, "max_global_concurrency": 1,
                                     "max_attempts": 1, "cancel_grace_seconds": 0,
                                     "zombie_max_seconds": 1, "default_timeout_seconds": 10},
            )
            channel = FakeChannel()
            dispatcher.register_channel(channel)
            await dispatcher.start()
            try:
                session_key = "webui:scope-test"
                entry = session_mgr.get_or_create(session_key)
                msg = InboundMessage(channel="webui", session_key=session_key,
                                     user_id="u", user_name="U", text="hello",
                                     message_id="m-scope-1")
                await dispatcher.on_inbound(msg)
                await asyncio.sleep(0.5)
                agent = entry.agent
                self.assertIsNotNone(agent)
                self.assertEqual(getattr(agent, "_runtime_task_source", ""), "")
                self.assertEqual(getattr(agent, "_runtime_goal_id", ""), "")
            finally:
                await dispatcher.stop()
                await session_mgr.stop()
        finally:
            disp_mod.create_gateway_agent = original

    async def test_wait_runtime_task_uses_enum_terminal_set(self):
        from gateway.dispatcher import Dispatcher
        dispatcher = object.__new__(Dispatcher)
        dispatcher._task_runtime = object()
        class Runtime:
            async def wait(self, task_id, timeout=None):
                return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED)
        dispatcher._session_runtime = Runtime()
        dispatcher._runtime_messages = {'done': object()}
        result = await dispatcher.wait_runtime_task('done')
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertNotIn('done', dispatcher._runtime_messages)


    async def test_subagent_archive_requires_terminal_state(self):
        from core.subagent import SubagentRuntime
        async def submit(**kwargs):
            envelope = TaskEnvelope.create(session_id=kwargs['session_id'], session_key=kwargs['session_key'], source='subagent', prompt=kwargs['prompt'], task_id='task_archive_child')
            self.store.create_task(envelope); return envelope.task_id
        blocker = asyncio.Event()
        async def wait(task_id): await blocker.wait()
        async def cancel(task_id, reason=''): pass
        runtime = SubagentRuntime(self.store, submit=submit, wait=wait, cancel=cancel)
        child = await runtime.create(parent_session_id='p_archive', parent_session_key='p', prompt='work')
        with self.assertRaises(ValueError): runtime.archive_child(child.child_id)
        blocker.set(); await asyncio.sleep(0)
        self.assertEqual(runtime.archive_child(child.child_id).status, 'archived')

    async def test_goal_archive_deletes_business_record(self):
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_archive', 'archive me')
        # 非终态可直接删除（archive 自动先取消，便于用户清理任意状态 Goal）
        archived = runtime.archive(goal.goal_id)
        self.assertIsNotNone(archived)          # 返回的内存快照仍可读
        self.assertEqual(archived.status.value, "cancelled")
        self.assertIsNone(runtime.get(goal.goal_id))  # 业务表记录已删除

    async def test_terminal_goal_archive_deletes_business_record(self):
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_archive2', 'archive me')
        done = runtime.complete(goal.goal_id)
        archived = runtime.archive(done.goal_id)
        self.assertIsNotNone(archived)          # 返回的内存快照仍可读
        self.assertIsNone(runtime.get(done.goal_id))  # 业务表记录已删除

    async def test_goal_continuation_snapshot_is_durable(self):
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_continuation', 'continue work')
        updated = runtime.update_continuation(goal.goal_id, {'cursor': 3, 'note': 'resume here'})
        self.assertEqual(updated.continuation['cursor'], 3)
        self.assertEqual(GoalRuntime(self.store).get(goal.goal_id).continuation['note'], 'resume here')

    async def test_goal_recovery_lists_active_paused_and_blocked(self):
        runtime = GoalRuntime(self.store)
        active = runtime.create('session_recover', 'active')
        paused = runtime.create('session_recover', 'paused')
        runtime.pause(paused.goal_id)
        blocked = runtime.create('session_recover', 'blocked')
        runtime.block(blocked.goal_id, {'type': 'waiting'})
        recovered = {goal.goal_id for goal in runtime.list_recoverable()}
        self.assertTrue({active.goal_id, paused.goal_id, blocked.goal_id}.issubset(recovered))

    async def test_goal_cancel_cancels_linked_plan(self):
        from core.plan import PlanManager
        from core.plan.models import Plan, PlanTask
        goals = GoalRuntime(self.store)
        goal = goals.create('session_goal_cancel', 'cancel plan')
        manager = PlanManager(self.store)
        plan = manager.create_preview(goal.session_id, {'steps': [{'description': 'work'}]}, source_prompt='work', goal_id=goal.goal_id)
        goal = goals.attach_plan(goal.goal_id, plan.plan_id)
        manager.approve(plan.plan_id); manager.activate(plan.plan_id)
        manager.cancel(plan.plan_id); cancelled = goals.cancel(goal.goal_id)
        self.assertEqual(manager.get(plan.plan_id).status.value, 'cancelled')
        self.assertEqual(cancelled.status.value, 'cancelled')

    async def test_clear_deletes_plan_business_records(self):
        from core.plan import PlanManager
        session_id = 'session_plan_clear'
        manager = PlanManager(self.store)
        plan = manager.create_preview(session_id, {'steps': [{'description': 'one'}, {'description': 'two'}]}, source_prompt='do', title='clear me')
        plan = manager.approve(plan.plan_id, actor='automatic')
        plan = manager.activate(plan.plan_id)
        while True:
            ready = manager.ready_tasks(plan.plan_id)
            if not ready:
                break
            task = ready[0]
            plan = manager.assign_task(plan.plan_id, task.plan_task_id, 'task-' + task.plan_task_id)
            plan = manager.start_task(plan.plan_id, task.plan_task_id)
            plan = manager.finish_task(plan.plan_id, task.plan_task_id, success=True, summary='done')
        self.assertTrue(plan.is_terminal)
        manager.archive_terminal(plan.plan_id)
        self.assertIsNone(manager.get(plan.plan_id))
        with self.store.connection() as connection:
            remaining = connection.execute('SELECT COUNT(*) FROM plan_tasks WHERE plan_id=?', (plan.plan_id,)).fetchone()[0]
        self.assertEqual(remaining, 0)

    async def test_plan_executor_runs_all_sequential_steps(self):
        from core.plan import PlanExecutor, PlanManager
        manager = PlanManager(self.store)
        plan = manager.create_preview('session_plan_all', {
            'steps': [{'description': 'one'}, {'description': 'two'}, {'description': 'three'}]
        }, source_prompt='do all')
        plan = manager.approve(plan.plan_id, actor='automatic')
        submitted = []
        async def submit_task(**kwargs):
            submitted.append(kwargs['plan_task'].plan_task_id)
            return kwargs['task_id']
        async def wait_task(task_id):
            return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED,
                              visible_text='done', summary='done')
        executor = PlanExecutor(manager, submit_task=submit_task, wait_task=wait_task,
                                publish=lambda *_args: None,
                                artifact_store=ArtifactStore(self.store))
        await executor.run(plan.plan_id)
        current = manager.get(plan.plan_id)
        self.assertEqual(submitted, ['step_1', 'step_2', 'step_3'])
        self.assertEqual(current.status.value, 'completed')
        self.assertTrue(all(task.status.value == 'completed' for task in current.tasks))


    async def test_clear_goal_cascades_linked_plans(self):
        from core.plan import PlanManager
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_goal_clear', 'clear cascades')
        manager = PlanManager(self.store)
        plan = manager.create_preview(goal.session_id, {'steps': [{'description': 'child'}]}, source_prompt='x', goal_id=goal.goal_id)
        runtime.complete(goal.goal_id)
        runtime.archive(goal.goal_id)
        self.assertIsNone(runtime.get(goal.goal_id))
        self.assertIsNone(manager.get(plan.plan_id))

    async def test_goal_reconciles_terminal_linked_plan(self):
        from core.plan.models import Plan, PlanStatus, PlanTask
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_plan_goal', 'finish plan')
        plan = Plan.create(session_id='session_plan_goal', title='P', tasks=[PlanTask('step', 'do')], goal_id=goal.goal_id)
        plan.status = PlanStatus.COMPLETED
        completed = runtime.reconcile_plan(plan)
        self.assertEqual(completed.status.value, 'completed')

    async def test_goal_quality_failure_blocks_completion(self):
        runtime = GoalRuntime(self.store, quality_gate=QualityGate(ArtifactStore(self.store)))
        goal = runtime.create('session_quality', 'prove a condition')
        blocked = runtime.complete(goal.goal_id, text='not enough', acceptance=[{'type': 'contains', 'value': 'required'}])
        self.assertEqual(blocked.status.value, 'blocked')
    async def test_cleared_goal_detaches_artifacts_for_retention(self):
        from core.runtime import RetentionManager
        from core.runtime.models import RuntimeEvent
        artifact_store = ArtifactStore(self.store)
        runtime = GoalRuntime(self.store)
        goal = runtime.create('session_archived', 'done')
        runtime.complete(goal.goal_id)
        artifact = artifact_store.create_text(session_id='session_archived', name='old.md', content='old')
        data = artifact.to_dict(); data['goal_id'] = goal.goal_id; data['created_at'] = '2000-01-01T00:00:00+00:00'
        self.store.save_artifact(data, RuntimeEvent.create('artifact.aged', session_id='session_archived'))
        runtime.archive(goal.goal_id)   # 清除 Goal 时同时解除其 artifact 引用
        result = RetentionManager(self.store, artifact_store, terminal_days=1, artifact_days=1).collect(dry_run=False)
        self.assertIn(artifact.artifact_id, result['deleted_artifacts'])

    async def test_child_session_lineage_projection(self):
        self.store.upsert_session('parent', 'parent-key')
        self.store.upsert_session('child', 'child-key', parent_session_id='parent', origin='subagent', subagent_mode='continuable')
        children = self.store.list_child_sessions('parent')
        self.assertEqual(children[0]['session_id'], 'child')
        self.assertEqual(children[0]['origin'], 'subagent')
        self.assertEqual(children[0]['subagent_mode'], 'continuable')

    async def test_dispatcher_persists_task_before_delivery_foreign_key(self):
        from gateway.channels.base import InboundMessage
        from gateway.dispatcher import Dispatcher
        from gateway.session import SessionManager
        class Channel:
            name = 'webui'; handles_chunking = True
            async def send_progress(self, message, text): pass
            async def send_reply(self, message, text): return True
        manager = SessionManager(max_sessions=2, persist=False, worker_pool_size=1)
        dispatcher = Dispatcher(manager, runtime_store=self.store,
                                task_runtime_config={'enabled': True, 'max_global_concurrency': 1})
        dispatcher.register_channel(Channel())
        await dispatcher.start()
        try:
            message = InboundMessage(channel='webui', session_key='webui:delivery-order',
                user_id='u', user_name='U', text='/stats', message_id='delivery-order-message')
            await dispatcher.on_inbound(message)
            for _ in range(20):
                rows = self.store.list_channel_deliveries()
                if rows: break
                await asyncio.sleep(0.01)
            self.assertEqual(len(rows), 1)
            self.assertIsNotNone(self.store.get_task(rows[0]['task_id']))
        finally:
            await dispatcher.stop()
            await manager.stop()

    async def test_channel_delivery_replay_marks_delivered(self):
        envelope = TaskEnvelope.create(session_id='session_replay', session_key='replay-key', source='user', prompt='x')
        self.store.create_task(envelope)
        self.store.transition_task(envelope.task_id, TaskStatus.LEASED)
        self.store.transition_task(envelope.task_id, TaskStatus.RUNNING)
        self.store.transition_task(envelope.task_id, TaskStatus.COMPLETED, result=TaskResult(task_id=envelope.task_id, status=TaskStatus.COMPLETED, visible_text='done'))
        self.store.save_channel_delivery(delivery_id='replay', task_id=envelope.task_id, channel='fake', message_id='m', state='recovery_pending', context={'user_id': 'u'})
        class Channel:
            name = 'fake'
            async def send_reply(self, msg, text): self.text = text
        class Sessions: pass
        from gateway.dispatcher import Dispatcher
        dispatcher = Dispatcher(Sessions(), runtime_store=self.store, task_runtime_config={'enabled': True})
        channel = Channel(); dispatcher.register_channel(channel)
        await dispatcher.recover_channel_deliveries()
        self.assertIn('done', channel.text)
        self.assertEqual(self.store.list_channel_deliveries(states={'delivered'})[0]['delivery_id'], 'replay')

    async def test_channel_delivery_recovery_marks_terminal_pending_delivery(self):
        envelope = TaskEnvelope.create(session_id='session_recovery_delivery', session_key='key', source='user', prompt='x')
        self.store.create_task(envelope)
        self.store.transition_task(envelope.task_id, TaskStatus.LEASED)
        self.store.transition_task(envelope.task_id, TaskStatus.RUNNING)
        self.store.transition_task(envelope.task_id, TaskStatus.COMPLETED, result=TaskResult(task_id=envelope.task_id, status=TaskStatus.COMPLETED))
        self.store.save_channel_delivery(delivery_id='recover', task_id=envelope.task_id, channel='debug', message_id='m', state='accepted', context={})
        class Sessions: pass
        from gateway.dispatcher import Dispatcher
        dispatcher = Dispatcher(Sessions(), runtime_store=self.store, task_runtime_config={'enabled': True})
        await dispatcher.recover_channel_deliveries()
        self.assertEqual(self.store.list_channel_deliveries(states={'retry_pending'})[0]['delivery_id'], 'recover')

    async def test_channel_delivery_and_schema_v11(self):
        envelope = TaskEnvelope.create(session_id='session_delivery', session_key='key_delivery', source='user', prompt='delivery')
        self.store.create_task(envelope)
        self.store.save_channel_delivery(delivery_id='d1', task_id=envelope.task_id, channel='webui', message_id='m1', state='accepted', context={'key': 'value'})
        with self.store.connection() as connection:
            row = connection.execute("SELECT state, context_json FROM channel_delivery WHERE delivery_id='d1'").fetchone()
            version = connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        self.assertEqual(row['state'], 'accepted')
        self.assertIn('value', row['context_json'])
        # v12：统一会话模型迁移（v13 起新增 turn_nodes.text_seq）；
        # v14：artifacts 关联列回填 + tasks(status)/sessions(parent_session_id) 等索引
        self.assertEqual(version, 16)  # v16：queue_items.images_json（图片信封）
    async def test_retention_deletes_unreferenced_terminal_data(self):
        from core.runtime import RetentionManager
        from core.runtime.models import utc_now
        artifact_store = ArtifactStore(self.store)
        envelope = TaskEnvelope.create(session_id='session_retention', session_key='key_retention', source='user', prompt='old')
        self.store.create_task(envelope)
        self.store.transition_task(envelope.task_id, TaskStatus.LEASED)
        self.store.transition_task(envelope.task_id, TaskStatus.RUNNING)
        result = TaskResult(task_id=envelope.task_id, status=TaskStatus.COMPLETED, finished_at='2000-01-01T00:00:00+00:00')
        self.store.transition_task(envelope.task_id, TaskStatus.COMPLETED, result=result)
        import json
        snapshot = self.store.get_task(envelope.task_id)
        record = snapshot.record.to_dict(); record['finished_at'] = '2000-01-01T00:00:00+00:00'
        with self.store.connection() as connection:
            connection.execute('UPDATE tasks SET record_json=? WHERE task_id=?', (json.dumps(record), envelope.task_id))
        artifact = artifact_store.create_text(session_id='session_retention', name='old.txt', content='old')
        # Force only this otherwise unreferenced artifact old enough for collection.
        data = self.store.get_artifact(artifact.artifact_id); data['created_at'] = '2000-01-01T00:00:00+00:00'
        from core.runtime.models import RuntimeEvent
        self.store.save_artifact(data, RuntimeEvent.create('artifact.aged', session_id='session_retention'))
        collected = RetentionManager(self.store, artifact_store, terminal_days=1, artifact_days=1).collect(dry_run=False)
        self.assertIn(envelope.task_id, collected['deleted_tasks'])
        self.assertIn(artifact.artifact_id, collected['deleted_artifacts'])

    async def test_subagent_team_report_survives_new_runtime(self):
        from core.subagent import SubagentRuntime
        submitted = []
        async def submit(**kwargs):
            submitted.append(kwargs)
            envelope = TaskEnvelope.create(session_id=kwargs['session_id'], session_key=kwargs['session_key'], source='subagent', prompt=kwargs['prompt'], task_id='task_child')
            self.store.create_task(envelope)
            return 'task_child'
        async def wait(task_id): return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED, visible_text='child result')
        async def cancel(task_id, reason=''): pass
        first = SubagentRuntime(self.store, submit=submit, wait=wait, cancel=cancel)
        report = await first.create(parent_session_id='parent_1', parent_session_key='parent-key', prompt='work', mode='continuable')
        await asyncio.sleep(0)
        second = SubagentRuntime(self.store, submit=submit, wait=wait, cancel=cancel)
        recovered = second.get_report(report.child_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.status, 'completed')
        self.assertEqual(second.list_reports('parent_1')[0].child_id, report.child_id)

    async def test_subagent_parent_cancellation_recovers_durable_report(self):
        from core.subagent import SubagentRuntime
        async def submit(**kwargs):
            envelope = TaskEnvelope.create(session_id=kwargs['session_id'], session_key=kwargs['session_key'], source='subagent', prompt=kwargs['prompt'], task_id='task_cancelled_child')
            self.store.create_task(envelope)
            return envelope.task_id
        async def wait(task_id): return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED)
        cancelled = []
        async def cancel(task_id, reason=''): cancelled.append((task_id, reason))
        runtime = SubagentRuntime(self.store, submit=submit, wait=wait, cancel=cancel)
        report = await runtime.create(parent_session_id='parent_cancel', parent_session_key='parent-key', prompt='work')
        await runtime.cancel_parent('parent_cancel', 'parent requested')
        self.assertEqual(cancelled, [(report.task_id, 'parent requested')])

    async def test_scheduler_rejects_goal_without_explicit_enablement(self):
        from gateway.scheduler import Scheduler
        class Dispatcher:
            async def on_inbound(self, msg): raise AssertionError('disabled Goal must not dispatch')
        class Sessions: pass
        scheduler = Scheduler({'jobs': []}, Dispatcher(), Sessions())
        await scheduler._fire({'name': 'goal_job', 'capability': 'goal', 'prompt': 'work'}, 'manual')
        self.assertFalse(scheduler._running_jobs)
        self.assertEqual(scheduler._state['jobs']['goal_job']['last_status'], 'skipped_goal_disabled')

    async def test_heartbeat_defers_for_active_subagent_task(self):
        from gateway.heartbeat import Heartbeat
        envelope = TaskEnvelope.create(session_id='sub_session', session_key='sub_key', source='subagent', prompt='work')
        self.store.create_task(envelope)
        class Dispatcher: _runtime_store = self.store
        class Sessions:
            def is_busy(self, key): return False
        heartbeat = Heartbeat({'defer_when_busy': True, 'prompt_file': 'missing.md'}, Dispatcher(), Sessions())
        self.assertFalse(await heartbeat._maybe_beat(ignore_idle=True))
        self.assertEqual(heartbeat._skips, 1)

    async def test_heartbeat_stuck_busy_evicts_after_threshold(self):
        """连续 busy skip 达阈值 → error 日志 + 驱逐 entry 并复位计数。"""
        import tempfile
        from gateway.heartbeat import Heartbeat
        prompt = Path(self.tmp.name) / 'HEARTBEAT.md'
        prompt.write_text('- 检查\n', encoding='utf-8')

        class Dispatcher:
            _runtime_store = None
            def __init__(self):
                self.inbound = []
            async def on_inbound(self, msg):
                self.inbound.append(msg)

        class Sessions:
            def __init__(self):
                self.busy = True
                self.evictions = []
            def is_busy(self, key):
                return self.busy
            async def evict(self, key, save=False):
                self.evictions.append((key, save))
                self.busy = False
                return True

        dispatcher, sessions = Dispatcher(), Sessions()
        heartbeat = Heartbeat({'defer_when_busy': True,
                               'prompt_file': str(prompt),
                               'stuck_skip_limit': 2},
                              dispatcher, sessions)
        # 第 1 轮：未达阈值，仅 info 跳过，不驱逐
        with self.assertLogs('jk_agent.gateway', level='INFO') as logs:
            self.assertFalse(await heartbeat._maybe_beat(ignore_idle=True))
        self.assertEqual(sessions.evictions, [])
        self.assertEqual(heartbeat._busy_skips, 1)
        # 第 2 轮：达到阈值 → error 告警 + evict(save=True) 强制恢复
        with self.assertLogs('jk_agent.gateway', level='ERROR') as logs:
            self.assertFalse(await heartbeat._maybe_beat(ignore_idle=True))
        joined = '\n'.join(logs.output)
        self.assertIn('heartbeat:main', joined)
        self.assertIn('疑似卡死', joined)
        self.assertEqual(sessions.evictions, [('heartbeat:main', True)])
        # 驱逐后 busy=False：下一轮正常心跳触发，计数归零
        self.assertEqual(heartbeat._busy_skips, 0)
        self.assertTrue(await heartbeat._maybe_beat(ignore_idle=True))
        self.assertEqual([m.session_key for m in dispatcher.inbound],
                         ['heartbeat:main'])
        self.assertEqual(heartbeat._busy_skips, 0)

    async def test_heartbeat_normal_run_resets_busy_skip_counter(self):
        """心跳正常运行一次即复位连续忙碌计数。"""
        from gateway.heartbeat import Heartbeat

        class Dispatcher:
            _runtime_store = None
            async def on_inbound(self, msg):
                pass

        class Sessions:
            def __init__(self):
                self.busy = True
            def is_busy(self, key):
                return self.busy
            async def evict(self, key, save=False):
                return False

        sessions = Sessions()
        prompt = Path(self.tmp.name) / 'HB2.md'
        prompt.write_text('x\n', encoding='utf-8')
        heartbeat = Heartbeat({'defer_when_busy': True,
                               'prompt_file': str(prompt),
                               'stuck_skip_limit': 3},
                              Dispatcher(), sessions)
        for _ in range(2):  # 未达阈值（3）
            self.assertFalse(await heartbeat._maybe_beat(ignore_idle=True))
        self.assertEqual(heartbeat._busy_skips, 2)
        sessions.busy = False  # 会话恢复空闲
        self.assertTrue(await heartbeat._maybe_beat(ignore_idle=True))
        self.assertEqual(heartbeat._busy_skips, 0)
        # 之后再次忙碌需重新从 0 累计
        sessions.busy = True
        self.assertFalse(await heartbeat._maybe_beat(ignore_idle=True))
        self.assertEqual(heartbeat._busy_skips, 1)

    async def test_heartbeat_stuck_limit_config_fallback(self):
        """stuck_skip_limit 解析失败回退默认 3；无 handle 时仅升级告警。"""
        from gateway.heartbeat import Heartbeat
        self.assertEqual(
            Heartbeat({'stuck_skip_limit': 'abc'}, None, None).stuck_skip_limit, 3)
        self.assertEqual(
            Heartbeat({}, None, None).stuck_skip_limit, 3)
        self.assertEqual(
            Heartbeat({'stuck_skip_limit': 5}, None, None).stuck_skip_limit, 5)

        class NoEvict:
            def is_busy(self, key):
                return True

        heartbeat = Heartbeat({'defer_when_busy': True,
                               'prompt_file': 'missing.md',
                               'stuck_skip_limit': 1},
                              None, NoEvict())
        with self.assertLogs('jk_agent.gateway', level='ERROR') as logs:
            self.assertFalse(await heartbeat._maybe_beat(ignore_idle=True))
        joined = '\n'.join(logs.output)
        self.assertIn('疑似卡死', joined)
        self.assertIn('无法强制恢复', joined)
        # 无句柄时计数保持，下一轮继续升级告警而非静默
        self.assertEqual(heartbeat._busy_skips, 1)

    async def test_v11_upgrade_deletes_only_plan_mode_workspace_sessions(self):
        with self.store.connection() as connection:
            connection.execute("INSERT INTO workspaces(workspace_id,name,project_path,created_at,updated_at) VALUES('workspace_upgrade','W','/tmp','x','x')")
            connection.execute("INSERT INTO workspace_sessions(session_id,workspace_id,session_key,chat_mode,created_at,updated_at) VALUES('retired_plan','workspace_upgrade','workspace:upgrade:retired','plan','x','x')")
            connection.execute("INSERT INTO workspace_sessions(session_id,workspace_id,session_key,chat_mode,created_at,updated_at) VALUES('current_chat','workspace_upgrade','workspace:upgrade:current','chat','x','x')")
            connection.execute("DELETE FROM schema_migrations WHERE version >= 11")
        RuntimeStore(self.store.path)
        with self.store.connection() as connection:
            rows = [row[0] for row in connection.execute('SELECT session_id FROM workspace_sessions ORDER BY session_id')]
        self.assertEqual(rows, ['current_chat'])

    async def test_retention_protects_referenced_artifact(self):
        from core.runtime import RetentionManager
        artifact_store = ArtifactStore(self.store)
        artifact = artifact_store.create_text(session_id='session_protected', name='goal.txt', content='keep', plan_id='plan_active')
        data = self.store.get_artifact(artifact.artifact_id); data['created_at'] = '2000-01-01T00:00:00+00:00'
        from core.runtime.models import RuntimeEvent
        self.store.save_artifact(data, RuntimeEvent.create('artifact.aged', session_id='session_protected'))
        collected = RetentionManager(self.store, artifact_store, terminal_days=1, artifact_days=1).collect(dry_run=False)
        self.assertIn(artifact.artifact_id, collected['protected'])
        self.assertIsNotNone(self.store.get_artifact(artifact.artifact_id))

if __name__ == '__main__': unittest.main()
