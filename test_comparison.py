import unittest
from comparison import record

class ComparisonTests(unittest.TestCase):
    def test_baseline_then_catchup(self):
        s = {'stories': {}}
        a = record(s, {'sent': ['old']}, 't0', 'check', {})
        self.assertTrue(a['baseline_only'])
        a = record(s, {'sent': ['old', 'new'], 'pending': [{'link': 'new'}]}, 't1', 'check', {})
        self.assertEqual(a['unmatched_urls_for_review'], 1)
        self.assertEqual(a['production_pending_urls'], 1)
        s['stories'] = {'one': {'articles': [{'link': 'new'}], 'message_id': None}}
        a = record(s, {'sent': ['new']}, 't2', 'check', {})
        self.assertEqual(a['unmatched_urls_for_review'], 0)
        self.assertEqual(a['matched_test_urls'], 1)
        self.assertEqual(a['test_waiting_stories'], 1)
        self.assertNotIn('old', s['comparison']['production_baseline'])
    def test_rotating_history_does_not_drop_observations(self):
        s = {}
        record(s, {'sent': []}, 't0', 'check', {})
        record(s, {'sent': ['one']}, 't1', 'check', {})
        a = record(s, {'sent': ['two']}, 't2', 'check', {})
        self.assertEqual(a['production_observed_since_start'], 2)

if __name__ == '__main__':
    unittest.main()
