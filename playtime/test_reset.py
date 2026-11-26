import unittest,copy
from playtime.serverless import reset_counter,handle
from playtime.model import apply_sample,new_state
from playtime.test_serverless import CONFIG,FRIEND,MemoryStore
from unittest.mock import patch
class Store:
 def __init__(self):self.state={'players':{'a':{'seconds':100}},'last_sample':100,'last_success':100,'cookies':{'auth':'synthetic'},'calendar':{'report':{}},'retry_at':300,'last_tick':99};self.released=False
 def acquire(self):return True
 def load(self):return copy.deepcopy(self.state)
 def save(self,state):self.state=state
 def release(self):self.released=True
class ResetTests(unittest.TestCase):
 def test_preserves_session_and_services(self):
  s=Store();before=copy.deepcopy(s.state);self.assertEqual(reset_counter(s)[0],200)
  for k in ('cookies','calendar','retry_at','last_tick','last_success'):self.assertEqual(s.state[k],before[k])
  self.assertEqual(s.state['players'],{});self.assertIsNone(s.state['last_sample']);self.assertTrue(s.released)
 def test_busy_does_not_load(self):
  s=Store();s.acquire=lambda:False;s.load=lambda:self.fail('Read while busy');self.assertEqual(reset_counter(s)[0],409)
 def test_failure_releases_lease(self):
  s=Store();s.save=lambda _:(_ for _ in ()).throw(RuntimeError())
  with self.assertRaises(RuntimeError):reset_counter(s)
  self.assertTrue(s.released)
 def test_cron_cannot_reset(self):
  with patch.dict('os.environ',{'PLAYTIME_ADMIN_SECRET':'a'*32,'CRON_SECRET':'b'*32}):self.assertEqual(handle('POST','Bearer '+'b'*32,'reset-confirmed')[0],401)

 def test_invalid_config_has_explicit_error(self):
  for secret in ('short', '\u044f'*32):
   with patch.dict('os.environ',{'PLAYTIME_ADMIN_SECRET':secret,'CRON_SECRET':'b'*32}):
    self.assertEqual(handle('POST','Bearer '+secret,'reset-confirmed'),(503,{'status':'admin_secret_invalid_format'}))


class ResetAccountingTests(unittest.TestCase):
    def test_reset_restarts_accounting_without_losing_verified_sample_time(self):
        state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], 1200, 1, 600)
        state = apply_sample(state, [FRIEND], 1260, 2, 600)
        self.assertEqual(state["players"]["usr_one"]["seconds"], 60)
        store = MemoryStore(state)
        reset_counter(store)
        self.assertEqual(store.state["last_success"], 1260)
        self.assertEqual(store.state["players"], {})
        self.assertIsNone(store.state["last_sample"])
        first = apply_sample(store.state, [FRIEND], 1320, 3, 600)
        self.assertEqual(first["players"]["usr_one"]["seconds"], 0)
        second = apply_sample(first, [FRIEND], 1380, 4, 600)
        self.assertEqual(second["players"]["usr_one"]["seconds"], 60)

    def test_reset_preserves_out_of_order_sample_guard(self):
        state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], 1200, 1, 600)
        store = MemoryStore(state)
        reset_counter(store)
        with self.assertRaises(ValueError):
            apply_sample(store.state, [FRIEND], 1200, 2, 600)

    def test_reset_does_not_invent_verified_history_for_new_counter(self):
        store = MemoryStore(new_state("bot", CONFIG["world_id"]))
        reset_counter(store)
        self.assertIsNone(store.state["last_success"])
        self.assertEqual(store.state["status"], "waiting_for_login")
        self.assertEqual(store.state["cookies"], {})
