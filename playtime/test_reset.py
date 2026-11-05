import unittest,copy
from playtime.serverless import reset_counter,handle
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
  for k in ('cookies','calendar','retry_at','last_tick'):self.assertEqual(s.state[k],before[k])
  self.assertEqual(s.state['players'],{});self.assertIsNone(s.state['last_sample']);self.assertIsNone(s.state['last_success']);self.assertTrue(s.released)
 def test_busy_does_not_load(self):
  s=Store();s.acquire=lambda:False;s.load=lambda:self.fail('Read while busy');self.assertEqual(reset_counter(s)[0],409)
 def test_failure_releases_lease(self):
  s=Store();s.save=lambda _:(_ for _ in ()).throw(RuntimeError())
  with self.assertRaises(RuntimeError):reset_counter(s)
  self.assertTrue(s.released)
 def test_cron_cannot_reset(self):
  with patch.dict('os.environ',{'PLAYTIME_ADMIN_SECRET':'a'*32,'CRON_SECRET':'b'*32}):self.assertEqual(handle('POST','Bearer '+'b'*32,'reset-confirmed')[0],401)
