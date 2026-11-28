"""Accepted shared client seam only; no Hub2 imports or synthetic packages."""
import hashlib
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
PATH=ROOT/'scripts/hub_alert_client.py'
spec=importlib.util.spec_from_file_location('accepted_backup_client_contract',PATH)
client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)

class AcceptedClientContractTests(unittest.TestCase):
 def test_reviewed_real_client_is_one_composed_shared_implementation(self):
  expected='5c81a8f9585d21cd32b3d8fd9f8d2e002bc6f0f2e8e973aa9a47fb554060f50c'
  self.assertEqual(hashlib.sha256(PATH.read_bytes()).hexdigest(),expected)
  self.assertNotIn('Controller',vars(client));self.assertNotIn('FileLedger',vars(client))
  self.assertNotIn('Dispatcher',vars(client));self.assertNotIn('dispatch',vars(client))
  self.assertTrue(callable(client.run_source_session))

 def test_preserved_quota_factory_keeps_old_backup_refusal_abi(self):
  for role,parent in [('backup_pull',None),('backup_notifier',None),('backup_notifier','a'*32)]:
   with self.assertRaises(client.Refused):client.positive_only_context(role,socket_path='/fixture/absent.sock',parent_id=parent,env={})

 def test_partial_backup_frame_schema_does_not_create_admission(self):
  frame={'v':1,'invocation_id':'a'*32,'parent_id':None,'role':'backup_notifier',
   'phase':'decision','seq':1,'decision':'alert','condition':'backup_failed','flags':{'no_mail':True},
   'recipient_class':'dev','observer_ok':False}
  self.assertEqual(client.checked(frame),frame)
  self.assertNotIn('admit',vars(client.Client));self.assertNotIn('dispatch',vars(client.Client))

 def test_real_missing_socket_is_bounded_negative_and_sticky(self):
  c=client.Client('/fixture/no-broker.sock','a'*32,None,'backup_notifier')
  self.assertFalse(c.emit('begin'));self.assertTrue(c.dirty)
  self.assertFalse(c.emit('decision',decision='alert',condition='backup_failed',recipient_class='dev'))
  self.assertTrue(c.dirty);self.assertEqual(c.seq,2)

if __name__=='__main__':unittest.main(verbosity=2)
