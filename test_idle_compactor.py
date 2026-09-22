import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import socket
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import idle_compactor as m

NOW = 1800000000.0

def row(typ, payload, when):
    return dict(type=typ, payload=payload,
                timestamp=dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat())

def records(now=NOW):
    return [row('event_msg', {'type':'task_started','turn_id':'turn-1'}, now-1300),
            row('response_item', {'type':'message','role':'user','content':'PRIVATE'}, now-1290),
            row('token_usage_record', {'usage':{'input_tokens':60000,'cached_input_tokens':59000,
                    'output_tokens':1000}}, now-1230),
            row('event_msg', {'type':'token_count','info':{'last_token_usage':{
                'input_tokens':90000,'output_tokens':1000}}}, now-1225),
            row('event_msg', {'type':'task_complete','turn_id':'turn-1'}, now-1210)]

def state(a, tid='thread-1'):
    return dict(id=tid, hostId='local', resumeState='resumed', requests=[],
                threadRuntimeStatus={'type':'idle'}, latestTokenUsageInfo={'last':{
                    'inputTokens':a['input_tokens'],'outputTokens':a['output_tokens']}})

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root/'test.jsonl'
        self.home = self.root/'codex'
        self.home.mkdir()
        self.app = self.root/'ChatGPT.app'
        (self.app/'Contents').mkdir(parents=True)
        info=dict(zip(('CFBundleIdentifier','CFBundleShortVersionString','CFBundleVersion'),('com.openai.codex','26.908.40834','8881')))
        (self.app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        self.archive()
        self.config_root = self.root/'state'
        m.config_read(self.config_root)
        self.write(records())
        self.a = m.activity(self.file)
        self.ledger = m.Ledger(self.config_root)
        self.c = dict(m.DEFAULTS)

    def archive(self,versions=None):
        versions=versions or {'thread-owner-discovery':1,'thread-stream-following-changed':1,
                             'thread-stream-state-changed':11,'thread-follower-compact-thread':1}
        js=('const protocols='+json.dumps(versions)).encode()
        tree={'files':{'.vite':{'files':{'build':{'files':{'src.js':{'size':len(js),'offset':'0'}}}}}}}
        head=json.dumps(tree).encode()
        resources=self.app/'Contents/Resources';resources.mkdir(exist_ok=True)
        (resources/'app.asar').write_bytes(struct.pack('<4I',4,len(head)+8,len(head)+4,len(head))+head+js)
        m.compatibility._archive_versions.cache_clear()

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def write(self, rows):
        self.file.write_text(''.join(json.dumps(x)+'\n' for x in rows))

    def test_eligible(self):
        self.assertIsNone(m.eligibility(self.a,self.c,NOW))
        self.assertEqual(self.a['context_tokens'],61000)
        self.assertNotIn('PRIVATE',json.dumps(self.a))

    def test_modern_usage_not_double_counted(self):
        self.assertEqual(self.a['input_tokens'],60000)

    def test_legacy_fallback(self):
        self.write([r for r in records() if r['type']!='token_usage_record'])
        self.assertEqual(m.activity(self.file)['context_tokens'],91000)

    def test_running_and_new_input(self):
        for extra in [row('event_msg',{'type':'task_started','turn_id':'turn-2'},NOW-1205),
                      row('response_item',{'role':'user','type':'message'},NOW-1205)]:
            self.write(records()+[extra])
            self.assertEqual(m.eligibility(m.activity(self.file),self.c,NOW),'running-or-new-input')

    def test_idle_boundaries(self):
        for elapsed, expected in [(1199,'not-idle-long-enough'),(1200,None),(1499,None),(1500,'missed-cache-window'),(1740,'missed-cache-window'),(1800,'missed-cache-window'),(90000,'missed-cache-window')]:
            a={**self.a,'usage_timestamp':NOW-elapsed}
            self.assertEqual(m.eligibility(a,self.c,NOW),expected)

    def test_compacted_after_completion(self):
        a={**self.a,'compacted':self.a['completed']+1}
        self.assertEqual(m.eligibility(a,self.c,NOW),'already-compacted')

    def test_invalid_missing_or_future_usage_time_never_dispatches(self):
        for timestamp in (float('nan'),float('inf'),float('-inf')):
            self.assertEqual(m.cache_window_reason(timestamp,self.c,NOW),'invalid-cache-clock')
        for timestamp in (0,NOW+100):
            self.assertEqual(m.cache_window_reason(timestamp,self.c,NOW),'not-idle-long-enough')

    def test_manual_cold_override_removed(self):
        argv=['idle_compactor.py','--state',str(self.config_root),'compact','thread-1','--allow-cold']
        with patch.object(m.sys,'argv',argv),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:m.main()
        self.assertEqual(result.exception.code,2)
        self.assertEqual(self.ledger.summary(),[])

    def test_compaction_before_turn_complete_is_not_repeated(self):
        a={**self.a,'compacted':self.a['completed']-0.017}
        self.assertEqual(m.eligibility(a,self.c,NOW),'already-compacted')
        a['usage_timestamp']=a['compacted']+1
        self.assertIsNone(m.eligibility(a,self.c,NOW))

    def test_context_bounds(self):
        for n in [1023,1000001]:
            self.assertEqual(m.eligibility({**self.a,'context_tokens':n},self.c,NOW),'context-outside-configured-range')

    def test_truncated_record_ignored(self):
        with self.file.open('a') as f:f.write('{"type":')
        self.assertEqual(m.activity(self.file)['fingerprint'],self.a['fingerprint'])

    def test_corrupt_record_fails_closed(self):
        with self.file.open('a') as f:f.write('not-json\n')
        with self.assertRaises(m.GuardError):m.activity(self.file)

    def test_active_pending_wrong_host_mismatch(self):
        for update in [dict(threadRuntimeStatus={'type':'active'}),dict(requests=[{}]),
                       dict(hostId='remote'),dict(resumeState='needs_resume'),dict(sideConversation=True),
                       dict(latestTokenUsageInfo={'last':{}})]:
            s={**state(self.a),**update}
            with self.assertRaises(m.GuardError):m.live_guard(s,self.a,self.c,'thread-1')
        self.assertEqual(m.live_guard(state(self.a),self.a,self.c,'thread-1'),61000)

    def test_reservation_blocks_all_other_calls(self):
        aid=self.ledger.reserve({'id':'thread-1'},self.a,self.c,NOW)
        self.assertEqual(self.ledger.reason('thread-2','turn-2',self.c,NOW),'unresolved-attempt-review-required')
        self.ledger.status(aid,'completed')
        self.assertEqual(self.ledger.reason('thread-1','turn-1',self.c,NOW),'already-attempted-this-turn')
        self.assertIsNone(self.ledger.reason('thread-1','turn-2',self.c,NOW))

    def test_same_task_new_turns_have_no_cooldown_after_restart(self):
        for i in range(50):
            aid=self.ledger.reserve({'id':'thread-1'},{**self.a,'turn_id':str(i)},self.c,NOW+i)
            self.ledger.status(aid,'completed')
        self.ledger.close();self.ledger=m.Ledger(self.config_root)
        self.assertEqual(self.ledger.reason('thread-1','0',self.c,NOW+100),'already-attempted-this-turn')
        self.assertEqual(self.ledger.reason('thread-1','51',self.c,NOW+100),'daily-cap')
        self.assertIsNone(self.ledger.reason('thread-1','51',self.c,NOW+86400))

    def test_legacy_cooldown_is_ignored(self):
        m.atomic_json(self.config_root/'config.json',{**self.c,'cooldown_seconds':86400})
        config=m.config_read(self.config_root)
        self.assertNotIn('cooldown_seconds',config)
        aid=self.ledger.reserve({'id':'thread-1'},self.a,config,NOW)
        self.ledger.status(aid,'completed')
        self.assertIsNone(self.ledger.reason('thread-1','turn-2',config,NOW+1500))

    def test_previous_default_window_migrates_without_changing_approval_or_limits(self):
        for enabled in (True, False):
            old={**self.c,'idle_seconds':1500,'latest_start_seconds':1740,
                 'enabled':enabled,'metered_automation_approved':enabled,
                 'max_per_day':7,'max_per_month':20,'exclude_threads':['private-task']}
            m.atomic_json(self.config_root/'config.json',old)
            c=m.config_read(self.config_root)
            self.assertEqual(c,{**old,'idle_seconds':1200,'latest_start_seconds':1500})
            # Reading cannot clobber a concurrent pause/settings write.
            self.assertEqual(json.loads((self.config_root/'config.json').read_text()),old)

    def test_custom_window_is_preserved(self):
        custom={**self.c,'idle_seconds':900,'latest_start_seconds':1440}
        m.atomic_json(self.config_root/'config.json',custom)
        self.assertEqual(m.config_read(self.config_root),custom)

    def test_expired_model_usage_cannot_be_refreshed_by_nonmodel_activity(self):
        self.write(records(NOW-600)+[row('event_msg',{'type':'task_complete','turn_id':'turn-1'},NOW-1500)])
        a=m.activity(self.file)
        self.assertEqual(m.eligibility(a,self.c,NOW),'missed-cache-window')
        class Fake:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):return state(a)
            def compact(self,*args):raise AssertionError('must not dispatch')
        m.atomic_json(self.config_root/'config.json',{**self.c,'enabled':True,'metered_automation_approved':True})
        for automatic in (False,True):
            with patch.object(m.time,'time',return_value=NOW),self.assertRaisesRegex(m.GuardError,'missed-cache-window'):
                m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                    self.home,self.app,self.ledger,automatic=automatic,desktop_factory=Fake)
        self.assertEqual(self.ledger.summary(),[])

    def test_scan_expired_window_blocks_automatic_and_manual(self):
        self.write(records(NOW-600))
        m.atomic_json(self.config_root/'config.json',{**self.c,'enabled':True,
            'metered_automation_approved':True,'cooldown_seconds':86400})
        candidate={'id':'thread-1','path':self.file}
        for execute in (False,True):
            for tid in (None,'thread-1'):
                with patch.object(m,'candidates',return_value=[candidate]),patch.object(m.time,'time',return_value=NOW),patch.object(m,'Desktop') as desktop:
                    result=m.scan(self.config_root,self.home,self.app,execute=execute,thread_id=tid)
                desktop.assert_not_called()
                self.assertEqual(result[0]['decision'],'missed-cache-window')
        self.assertEqual(self.ledger.summary(),[])

    def test_enable_cleans_legacy_cooldown_and_keeps_approved_limits(self):
        m.atomic_json(self.config_root/'config.json',{**self.c,'enabled':True,'cooldown_seconds':86400})
        argv=['idle_compactor.py','--state',str(self.config_root),'enable',
              '--accept-metered-compaction','--max-per-day','50','--max-per-month','none']
        output=io.StringIO()
        with patch.object(m.sys,'argv',argv),contextlib.redirect_stdout(output):m.main()
        self.assertEqual(json.loads(output.getvalue())['compaction']['policy'],'time-based-best-effort')
        config=json.loads((self.config_root/'config.json').read_text())
        self.assertTrue(config['enabled'])
        self.assertTrue(config['metered_automation_approved'])
        self.assertNotIn('cooldown_seconds',config)
        self.assertEqual(config['max_per_day'],50)
        self.assertIsNone(config['max_per_month'])

    def test_duplicate_usage_records_do_not_refresh_cache_clock(self):
        original=records()[2]
        original['payload']['response_id']='response-1'
        self.write(records()[:2]+[original]+records()[3:]+[{**original,'timestamp':row('',{},NOW)['timestamp']}])
        self.assertEqual(m.activity(self.file)['usage_timestamp'],NOW-1230)
        legacy=[r for r in records() if r['type']!='token_usage_record']
        repeated={**legacy[2],'timestamp':row('',{},NOW)['timestamp']}
        self.write(legacy+[repeated])
        self.assertEqual(m.activity(self.file)['usage_timestamp'],NOW-1225)

    def test_preflight_delay_past_window_does_not_reserve(self):
        a=self.a;clock=[NOW]
        class Fake:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):
                clock[0]=a['usage_timestamp']+1500
                return state(a)
            def compact(self,*args):raise AssertionError('must not dispatch')
        with patch.object(m.time,'time',side_effect=lambda:clock[0]),self.assertRaisesRegex(m.GuardError,'missed-cache-window'):
            m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                self.home,self.app,self.ledger,desktop_factory=Fake)
        self.assertEqual(self.ledger.summary(),[])

    def test_reservation_delay_past_window_does_not_dispatch_or_block_others(self):
        a=self.a;clock=[NOW];reserve=self.ledger.reserve
        class Fake:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):return state(a)
            def compact(self,*args):raise AssertionError('must not dispatch')
        def slow_reserve(*args):
            result=reserve(*args)
            clock[0]=a['usage_timestamp']+1500
            return result
        with patch.object(m.time,'time',side_effect=lambda:clock[0]),patch.object(self.ledger,'reserve',side_effect=slow_reserve):
            result=m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                self.home,self.app,self.ledger,desktop_factory=Fake)
        self.assertEqual(result['status'],'skipped-no-dispatch')
        self.assertIsNone(self.ledger.reason('other','other',self.c,NOW))
        self.assertEqual(self.ledger.reconcile(self.home),[])

    def test_limits_and_restart(self):
        self.c.update(max_per_day=2,max_per_month=20)
        for i in range(2):
            aid=self.ledger.reserve({'id':str(i)},{**self.a,'turn_id':str(i)},self.c,NOW)
            self.ledger.status(aid,'completed')
        self.ledger.close();self.ledger=m.Ledger(self.config_root)
        self.assertEqual(self.ledger.reason('third','third',self.c,NOW),'daily-cap')
        c={**self.c,'max_per_day':10,'max_per_month':2}
        self.assertEqual(self.ledger.reason('third','third',c,NOW),'monthly-cap')

    def test_fifty_daily_and_no_monthly_cap_survive_restart(self):
        c={**self.c,'max_per_day':50,'max_per_month':None}
        # Previous days in the same month do not impose a hidden monthly ceiling.
        for i in reversed(range(55)):
            aid=self.ledger.reserve({'id':'older'+str(i)},self.a,c,NOW-86400*(1+i//50))
            self.ledger.status(aid,'completed')
        for i in range(50):
            aid=self.ledger.reserve({'id':'today'+str(i)},self.a,c,NOW)
            self.ledger.status(aid,'completed')
        self.ledger.close();self.ledger=m.Ledger(self.config_root)
        self.assertEqual(self.ledger.reason('next','next',c,NOW),'daily-cap')
        self.assertIsNone(self.ledger.reason('next','next',c,NOW+86400))

    def test_monthly_cap_optional_and_validated(self):
        for value in (None,1,5000):
            m.atomic_json(self.config_root/'config.json',{**m.DEFAULTS,'max_per_month':value})
            self.assertEqual(m.config_read(self.config_root)['max_per_month'],value)
        for value in (0,-1,True,'none'):
            m.atomic_json(self.config_root/'config.json',{**m.DEFAULTS,'max_per_month':value})
            with self.assertRaises(m.GuardError):m.config_read(self.config_root)
        self.assertIsNone(m.monthly_cap('none'))
        self.assertEqual(m.monthly_cap('20'),20)

    def test_single_instance_lock(self):
        with m.exclusive(self.config_root):
            with self.assertRaises(m.GuardError):
                with m.exclusive(self.config_root):pass

    def test_default_disabled_and_invalid_config(self):
        self.assertFalse(m.config_read(self.config_root)['enabled'])
        for update in [{'max_per_day':100},{'idle_seconds':1800},{'enabled':'false'}, {'allow_threads':'*'}]:
            m.atomic_json(self.config_root/'config.json',{**m.DEFAULTS,**update})
            with self.assertRaises(m.GuardError):m.config_read(self.config_root)

    def test_watch_publishes_liveness_before_a_slow_first_scan(self):
        def scan(*args,**kwargs):
            health=json.loads((self.config_root/'worker-health.json').read_text())
            self.assertEqual(health['status'],'scanning')
            self.assertEqual(health['version'],m.VERSION)
            (self.config_root/'restart-request').touch()
            return []
        argv=['idle_compactor.py','--state',str(self.config_root),'watch']
        with patch.object(m.sys,'argv',argv),patch.object(m,'scan',side_effect=scan),contextlib.redirect_stdout(io.StringIO()):
            m.main()
        self.assertEqual(json.loads((self.config_root/'worker-health.json').read_text())['status'],'running')

    def test_future_build_accepted_by_capabilities(self):
        p=self.app/'Contents/Info.plist';x=plistlib.loads(p.read_bytes());x['CFBundleVersion']='future'
        p.write_bytes(plistlib.dumps(x))
        self.assertEqual(m.verify_app(self.app)[2],'future')

    def test_unknown_mutation_contract_blocks(self):
        self.archive({'thread-owner-discovery':1,'thread-stream-following-changed':1,
                      'thread-stream-state-changed':45,'thread-follower-compact-thread':9})
        with self.assertRaises(m.GuardError):m.verify_app(self.app)

    def test_new_snapshot_protocol_can_be_probed(self):
        self.archive({'thread-owner-discovery':1,'thread-stream-following-changed':1,
                      'thread-stream-state-changed':45,'thread-follower-compact-thread':1})
        self.assertEqual(m.compatibility.read_protocol(self.app)['versions']['thread-stream-state-changed'],45)

    def test_older_idle_shape(self):
        s=state(self.a);del s['threadRuntimeStatus']
        s['turns']=[{'turnId':self.a['turn_id'],'status':'completed'}]
        self.assertEqual(m.live_guard(s,self.a,self.c,'thread-1'),61000)
        s['turns'][-1]['status']='inProgress'
        with self.assertRaises(m.GuardError):m.live_guard(s,self.a,self.c,'thread-1')

    def test_candidate_scope(self):
        sessions=self.home/'sessions';sessions.mkdir()
        db=sqlite3.connect(self.home/'state_99.sqlite')
        db.execute('CREATE TABLE threads(id,rollout_path,source,model,archived,updated_at)')
        data=[('ok',str(sessions/'one'),'desktop','m',0,NOW),
              ('archived',str(sessions/'two'),'vscode','m',1,NOW),
              ('subagent',str(sessions/'three'),'{"subagent":{}}','m',0,NOW),
              ('external',str(self.file),'vscode','m',0,NOW),
              ('stale',str(sessions/'old'),'vscode','m',0,NOW-9999)]
        db.executemany('INSERT INTO threads VALUES(?,?,?,?,?,?)',data);db.commit();db.close()
        self.assertEqual([r['id'] for r in m.candidates(self.home,NOW)],['ok'])

    def test_no_completion_without_marker(self):
        a=self.a;test=self
        class Fake:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):return state(a)
            def compact(self,*args):pass
        with patch.object(m.time,'time',return_value=NOW):
            r=m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                self.home,self.app,self.ledger,desktop_factory=Fake,timeout=0)
        self.assertEqual(r['status'],'unverified-timeout-no-retry')
        self.assertEqual(self.ledger.reason('other','other',self.c,NOW),'unresolved-attempt-review-required')

    def test_success_requires_marker(self):
        a=self.a;test=self
        class Fake:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):return state(a)
            def compact(self,*args):test.write(records()+[row('compacted',{},NOW)])
        with patch.object(m.time,'time',return_value=NOW):
            r=m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                self.home,self.app,self.ledger,desktop_factory=Fake)
        self.assertEqual(r['status'],'completed')

    def test_kill_switch_blocks_dispatch(self):
        a=self.a
        class Fake:
            called=False
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):return state(a)
            def compact(self,*args):Fake.called=True
        with self.assertRaises(m.GuardError):
            m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                self.home,self.app,self.ledger,automatic=True,desktop_factory=Fake)
        self.assertFalse(Fake.called)
        self.assertEqual(self.ledger.summary(),[])

    def test_activity_changed_prevents_dispatch(self):
        a=self.a;test=self
        class Fake:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):
                test.write(records()+[row('response_item',{'role':'user'},NOW)])
                return state(a)
            def compact(self,*args):raise AssertionError('must not dispatch')
        with self.assertRaises(m.GuardError):
            m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                self.home,self.app,self.ledger,desktop_factory=Fake)
        self.assertEqual(self.ledger.summary(),[])

    def test_uncertain_request_never_retried(self):
        a=self.a
        class Fake:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):return state(a)
            def compact(self,*args):raise TimeoutError()
        with patch.object(m.time,'time',return_value=NOW):
            with self.assertRaises(TimeoutError):
                m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                    self.home,self.app,self.ledger,desktop_factory=Fake)
        self.assertEqual(self.ledger.summary(),[{'status':'uncertain-no-retry','count':1}])

    def test_scan_resolves_late_marker_without_another_model_call(self):
        aid=self.ledger.reserve({'id':'thread-1'},self.a,self.c,NOW-30)
        self.ledger.status(aid,'unverified-timeout-no-retry')
        self.write(records()+[row('compacted',{},NOW-10)])
        m.atomic_json(self.config_root/'config.json',{**self.c,'enabled':True,'metered_automation_approved':True})
        candidate={'id':'thread-1','path':self.file}
        with patch.object(m,'candidates',return_value=[candidate]),patch.object(m.time,'time',return_value=NOW),patch.object(m,'Desktop') as desktop:
            result=m.scan(self.config_root,self.home,self.app,execute=True)
        desktop.assert_not_called()
        self.assertEqual(result[0]['decision'],'already-compacted')
        self.assertEqual(self.ledger.summary(),[{'status':'completed','count':1}])

    def test_scope_change_blocks_dispatch(self):
        a=self.a;test=self
        class Fake:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):
                m.atomic_json(test.config_root/'config.json',{**m.DEFAULTS,'exclude_threads':['thread-1']})
                return state(a)
            def compact(self,*args):raise AssertionError('must not dispatch')
        with self.assertRaises(m.GuardError):
            m.run_compaction({'id':'thread-1','path':self.file},a,self.c,self.config_root,
                self.home,self.app,self.ledger,desktop_factory=Fake)
        self.assertEqual(self.ledger.summary(),[])

    def test_uninstall_reports_failure_and_preserves_plist(self):
        from types import SimpleNamespace
        path=self.root/'Library/LaunchAgents'/(m.LABEL+'.plist')
        path.parent.mkdir(parents=True);path.write_text('fixture')
        with patch.object(m.sys,'platform','darwin'),patch.object(m.Path,'home',return_value=self.root):
            with patch.object(m.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
                with self.assertRaises(m.GuardError):m.launch_agent(None,remove=True)
            self.assertTrue(path.exists())
            with patch.object(m.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
                self.assertFalse(m.launch_agent(None,remove=True)['installed'])
            self.assertFalse(path.exists())

    def test_usage_measurement_unknown_and_deduplicated(self):
        sessions=self.home/'sessions';sessions.mkdir()
        rollout=sessions/'session.jsonl'
        db=sqlite3.connect(self.home/'state_5.sqlite')
        db.execute('CREATE TABLE threads(id,rollout_path,source,model,archived,updated_at)')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',('thread-1',str(rollout),'vscode','m',0,NOW))
        db.commit();db.close()
        aid=self.ledger.reserve({'id':'thread-1'},self.a,self.c,NOW)
        compact_usage=row('token_usage_record',{'response_id':'compact','usage':{
            'input_tokens':61000,'cached_input_tokens':60000,'output_tokens':1200}},NOW+1)
        resumed=row('token_usage_record',{'response_id':'resume','usage':{
            'input_tokens':12000,'cached_input_tokens':1000,'output_tokens':100}},NOW+2000)
        records_=[compact_usage,compact_usage,row('compacted',{},NOW+2),
                  row('response_item',{'role':'user'},NOW+1990),resumed]
        rollout.write_text(''.join(json.dumps(r)+'\n' for r in records_))
        metrics=m.measurement(self.home,self.ledger)[0]
        self.assertEqual(metrics['compaction_usage']['input_tokens'],61000)
        self.assertEqual(metrics['first_resume_usage']['noncached_input_tokens'],11000)
        self.assertEqual(metrics['cache_age_seconds'],1230)
        self.assertEqual(metrics['configured_trigger_seconds'],1200)
        self.assertEqual(metrics['configured_cutoff_seconds'],1500)
        self.assertEqual(metrics['compaction_duration_seconds'],2)
        self.assertAlmostEqual(metrics['compaction_usage']['cached_input_fraction'],60000/61000)
        self.assertNotIn('PRIVATE',json.dumps(metrics))
        rollout.write_text(json.dumps(row('compacted',{},NOW+2))+'\n')
        self.ledger.close();self.ledger=m.Ledger(self.config_root)
        self.assertEqual(m.measurement(self.home,self.ledger)[0],metrics)
        # Even after archival/removal, measurements already saved survive.
        rollout.unlink()
        with patch.object(m,'candidates',return_value=[]):
            self.assertEqual(m.measurement(self.home,self.ledger)[0],metrics)

    def test_missing_or_invalid_usage_stays_unknown(self):
        self.ledger.reserve({'id':'thread-1'},self.a,self.c,NOW)
        metrics=m.measurement(self.home,self.ledger)[0]
        self.assertIsNone(metrics['compaction_usage'])
        self.assertIsNone(metrics['first_resume_usage'])
        for usage in ({}, {'input_tokens':100,'output_tokens':10},
                      {'input_tokens':100,'cached_input_tokens':110,'output_tokens':10},
                      {'input_tokens':100,'cached_input_tokens':50,'output_tokens':None}):
            self.assertIsNone(m.usage_counters(usage))
            observations=m.observe_records([row('token_usage_record',{'usage':usage},NOW+1),
                                            row('compacted',{},NOW+2)],NOW)
            self.assertNotIn('compaction_usage',observations)

    def test_later_compaction_cannot_replace_a_saved_measurement(self):
        self.ledger.reserve({'id':'thread-1'},self.a,self.c,NOW)
        self.write([row('token_usage_record',{'usage':{
            'input_tokens':61000,'cached_input_tokens':60000,'output_tokens':100}},NOW+1),
            row('compacted',{},NOW+2)])
        with patch.object(m,'candidates',return_value=[{'path':self.file}]):
            first=m.measurement(self.home,self.ledger)[0]
            self.write([row('compacted',{},NOW+10000),row('response_item',{'role':'user'},NOW+10001),
                        row('token_usage_record',{'usage':{
                            'input_tokens':90000,'cached_input_tokens':0,'output_tokens':100}},NOW+10002)])
            second=m.measurement(self.home,self.ledger)[0]
        self.assertEqual(first,second)

    def test_first_resume_is_collected_on_a_later_pass(self):
        self.ledger.reserve({'id':'thread-1'},self.a,self.c,NOW)
        original=[row('token_usage_record',{'usage':{
            'input_tokens':61000,'cached_input_tokens':60000,'output_tokens':100}},NOW+1),
            row('compacted',{},NOW+2)]
        self.write(original)
        with patch.object(m,'candidates',return_value=[{'path':self.file}]):
            first=m.measurement(self.home,self.ledger)[0]
            self.write(original+[row('response_item',{'role':'user'},NOW+100),
                                 row('token_usage_record',{'usage':{
                                     'input_tokens':10000,'cached_input_tokens':5000,'output_tokens':50}},NOW+101)])
            second=m.measurement(self.home,self.ledger)[0]
        self.assertEqual(first['compaction_usage'],second['compaction_usage'])
        self.assertIsNone(first['first_resume_usage'])
        self.assertEqual(second['first_resume_usage']['noncached_input_tokens'],5000)

    def test_background_measurement_throttle_batch_and_fairness(self):
        for i in range(6):
            aid=self.ledger.reserve({'id':str(i)},{**self.a,'turn_id':str(i)},self.c,NOW+i)
            self.ledger.status(aid,'completed')
        with patch.object(m,'candidates',return_value=[]) as candidates,patch.object(m.time,'time',return_value=NOW+100):
            m.measurement(self.home,self.ledger,refresh_limit=4)
            self.assertEqual(candidates.call_count,4)
            m.measurement(self.home,self.ledger,refresh_limit=4)
            self.assertEqual(candidates.call_count,6)
            m.measurement(self.home,self.ledger,refresh_limit=4)
            self.assertEqual(candidates.call_count,6)
        with patch.object(m,'candidates',return_value=[]) as candidates,patch.object(m.time,'time',return_value=NOW+400):
            m.measurement(self.home,self.ledger,refresh_limit=4)
            self.assertEqual(candidates.call_count,4)
        with patch.object(m,'candidates') as candidates,patch.object(m.time,'time',return_value=NOW+31*86400):
            m.measurement(self.home,self.ledger,refresh_limit=4)
            candidates.assert_not_called()

    def test_historical_age_backfill_does_not_invent_old_policy(self):
        self.ledger.db.execute("INSERT INTO attempts VALUES(1,'thread-1','old',?,'completed',61000,0,?)",(NOW,NOW+2))
        self.ledger.db.commit()
        self.write(records()+[row('token_usage_record',{'usage':{
            'input_tokens':61000,'cached_input_tokens':60000,'output_tokens':100}},NOW+1),
            row('compacted',{},NOW+2)])
        with patch.object(m,'candidates',return_value=[{'path':self.file}]):
            metric=m.measurement(self.home,self.ledger)[0]
        self.assertEqual(metric['cache_age_seconds'],1230)
        self.assertIsNone(metric['configured_trigger_seconds'])
        self.assertIsNone(metric['utility_version'])

    def test_background_logging_continues_when_adapter_is_blocked(self):
        def collect(*args):
            (self.config_root/'restart-request').touch()
        argv=['idle_compactor.py','--state',str(self.config_root),'watch']
        with patch.object(m.sys,'argv',argv),patch.object(m,'scan',side_effect=m.GuardError('unsupported')), \
             patch.object(m,'collect_measurements',side_effect=collect) as collect_mock, \
             contextlib.redirect_stdout(io.StringIO()):
            m.main()
        collect_mock.assert_called_once()

    def test_ipc_real_socket_framing_and_final_timing_guard(self):
        # Real socket framing with a fake server; no model call. Verify the
        # final transport guard both allows a timely request and rejects late ones.
        ipc=self.home/'ipc';ipc.mkdir(mode=0o700)
        path=ipc/'ipc.sock'
        server=socket.socket(socket.AF_UNIX);server.bind(str(path));path.chmod(0o600);server.listen(1)
        methods=[];errors=[];a=self.a
        def serve():
            try:
                conn,_=server.accept();conn.settimeout(3)
                def receive():
                    def read(n):
                        out=b''
                        while len(out)<n:
                            x=conn.recv(n-len(out))
                            if not x:raise EOFError()
                            out+=x
                        return out
                    return json.loads(read(struct.unpack('<I',read(4))[0]))
                def send(obj):
                    b=json.dumps(obj).encode();packet=struct.pack('<I',len(b))+b
                    conn.sendall(packet[:2]);conn.sendall(packet[2:9]);conn.sendall(packet[9:])
                while True:
                    q=receive();method=q.get('method');methods.append(method)
                    if q['type']=='request':
                        if method=='initialize':result={'clientId':'watcher'};owner='watcher'
                        elif method=='thread-owner-discovery':result={};owner='owner'
                        elif method=='thread-follower-compact-thread':result={'ok':True};owner='owner'
                        else:raise AssertionError(method)
                        send(dict(type='response',requestId=q['requestId'],resultType='success',
                                  result=result,handledByClientId=owner,method=method))
                    elif method=='thread-stream-following-changed':
                        if not q['params']['following']:break
                        send(dict(type='broadcast',method='thread-stream-state-changed',version=11,
                                  sourceClientId='owner',params=dict(hostId='local',conversationId='thread-1',
                                  change={'type':'snapshot','conversationState':state(a)})))
                conn.close()
            except Exception as e:errors.append(e)
            finally:server.close()
        t=threading.Thread(target=serve);t.start()
        with m.Desktop(self.home,self.app) as client:
            self.assertEqual(client.snapshot('thread-1')['id'],'thread-1')
            with patch.object(m.time,'time',return_value=NOW):
                client.compact(a['usage_timestamp'],self.c)
            for age in (1500,1740,1800,1801,90000):
                with patch.object(m.time,'time',return_value=a['usage_timestamp']+age):
                    with self.assertRaisesRegex(m.DispatchSkipped,'missed-cache-window'):
                        client.compact(a['usage_timestamp'],self.c)
            with patch.object(m.time,'time',return_value=a['usage_timestamp']+1800):
                with self.assertRaises(m.DispatchSkipped):
                    client.compact(a['usage_timestamp'],{**self.c,'latest_start_seconds':9999})
        t.join(4)
        self.assertFalse(t.is_alive());self.assertEqual(errors,[])
        self.assertEqual(methods.count('thread-follower-compact-thread'),1)

if __name__=='__main__':unittest.main()
