import json
import unittest
from robot_test_hub.source_status import StatusInbox


def status(sequence, **changes):
    return json.dumps(dict(schema_version=1,robot_id='robot-6391',runtime_mode='REAL',boot_id='boot-a',
        sequence=sequence,mode_generation=1,enabled=False,mode='disabled',operating_mode='TELEOP',
        transfer_allowed=True,robot_monotonic_ns=str(sequence*20000000),**changes))


class SourceStatusTests(unittest.TestCase):
    def setUp(self):
        self.now=100
        self.inbox=StatusInbox('robot-6391',clock=lambda:self.now)

    def send(self,number,**changes):
        value=json.loads(status(number));value.update(changes)
        return self.inbox.receive(json.dumps(value))

    def test_initial_retained_status_requires_advancement_and_polling_does_not_refresh(self):
        self.assertIsNone(self.send(100).enabled)
        self.assertFalse(self.send(101).enabled)
        self.now=1000
        self.assertEqual(self.inbox.status().observed_at,100)
        self.send(101)
        self.assertEqual(self.inbox.status().observed_at,100)

    def test_wrong_robot_runtime_units_schema_and_nonadvancing_fail_closed(self):
        for change in ({'robot_id':'other'},{'runtime_mode':'SIM'},{'schema_version':True},
                       {'enabled':'false'},{'robot_monotonic_ns':20000000},{'sequence':True},
                       {'enabled':True},{'mode':'teleop'}):
            with self.subTest(change=change):
                self.assertIsNone(self.send(1,**change).enabled)
                self.assertFalse(self.inbox.status().transfer_allowed)
        self.send(10);self.send(11)
        self.assertIsNone(self.send(10).enabled)

    def test_mode_generation_disconnect_and_boot_require_new_proof(self):
        self.send(1);self.send(2)
        self.assertIsNone(self.send(3,enabled=True,mode='teleop',transfer_allowed=False).enabled)
        self.assertTrue(self.send(4,enabled=True,mode='teleop',transfer_allowed=False,mode_generation=2).enabled)
        self.inbox.disconnect()
        self.assertIsNone(self.send(4,enabled=True,mode='teleop',transfer_allowed=False,mode_generation=2).enabled)
        self.assertFalse(self.send(5,mode_generation=3).enabled)
        self.assertIsNone(self.send(1,boot_id='boot-b').enabled)
        self.assertFalse(self.send(2,boot_id='boot-b').enabled)

    def test_first_newer_cached_value_after_reconnect_is_not_a_fresh_pair(self):
        self.send(1);self.send(2)
        self.inbox.disconnect()
        self.assertIsNone(self.send(50).enabled)
        self.assertIsNone(self.send(50).enabled)
        self.assertFalse(self.send(51).enabled)
