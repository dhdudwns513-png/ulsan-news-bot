import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import story_mode as sm

NOW = datetime(2026, 9, 16, 12, tzinfo=timezone(timedelta(hours=9)))


def article(title='HD현대중공업 임단협 노사 교섭 재개', url='https://example.com/1', priority=2):
    return dict(title=title, link=url, pub=NOW, source='테스트 언론', priority=priority)


class FakeTelegram:
    def __init__(self):
        self.sends, self.edits = [], []
        self.fail = False
        self.missing = False
    def identity(self):
        return dict(bot_id=1, chat_id=-10012345, username='test_channel')
    def send(self, text, silent=True):
        if self.fail:
            raise RuntimeError('failed')
        self.sends.append((text, silent))
        return len(self.sends)
    def edit(self, mid, text):
        if self.fail:
            raise RuntimeError('failed')
        self.edits.append((mid, text))
        return not self.missing


class StoriesTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.client = FakeTelegram()
        self.saved = []
    def save(self, s):
        self.saved.append(copy.deepcopy(s))
    def ingest(self, *articles):
        sm.ingest(self.state, articles, NOW, {})
    def deliver(self, mode='check', quiet=False):
        return sm.deliver(self.state, mode, NOW, {}, self.client, self.save, quiet)
    def test_same_event_groups_across_runs(self):
        self.ingest(article())
        self.deliver()
        self.ingest(article('HD현대중공업 임단협 노사 교섭 재개 논의', 'https://example.com/2'))
        self.deliver()
        self.assertEqual(len(self.state['stories']), 1)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(len(self.client.edits), 1)
        self.assertIn('관련 기사 2건', self.client.edits[0][1])
    def test_same_company_different_event_does_not_merge(self):
        self.ingest(article(), article('HD현대중공업 신규 공장 투자 발표', 'https://example.com/2'))
        self.assertEqual(len(self.state['stories']), 2)
    def test_same_words_outside_window_separate(self):
        self.ingest(article())
        later = article(url='https://example.com/2'); later['pub'] = NOW + timedelta(days=4)
        self.ingest(later)
        self.assertEqual(len(self.state['stories']), 2)
    def test_duplicate_url_does_not_edit_or_send_again(self):
        self.ingest(article()); self.deliver()
        self.ingest(article()); self.deliver()
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(len(self.client.edits), 0)
    def test_ordinary_news_waits_for_briefing(self):
        self.ingest(article(priority=1)); self.deliver()
        self.assertFalse(self.client.sends)
        self.deliver('briefing')
        self.assertEqual(len(self.client.sends), 2)
        self.assertIn('https://t.me/test_channel/1', self.client.sends[-1][0])
    def test_major_development_alert_once(self):
        self.ingest(article()); self.deliver()
        self.ingest(article('HD현대중공업 임단협 노사 교섭 잠정합의', 'https://example.com/2'))
        self.deliver(); self.deliver()
        self.assertEqual(len(self.client.sends), 2)
        self.assertIn('주요 후속 보도', self.client.sends[-1][0])
    def test_proposal_is_not_completed_event(self):
        self.assertFalse(sm.important('임단협 합의 촉구'))
        self.assertFalse(sm.important('임단협 합의 가능성'))
    def test_failed_send_not_checkpointed_as_delivered(self):
        self.ingest(article()); self.client.fail = True
        with self.assertRaises(RuntimeError): self.deliver()
        self.assertFalse(self.saved)
        self.assertIsNone(next(iter(self.state['stories'].values()))['message_id'])
    def test_deleted_card_recreated(self):
        self.ingest(article()); self.deliver(); self.client.missing = True
        self.ingest(article(url='https://example.com/2')); self.deliver()
        self.assertEqual(len(self.client.sends), 2)
    def test_route_change_stops_before_delivery(self):
        self.state['story_route'] = dict(bot_id=999, chat_id=-10012345)
        self.ingest(article())
        with self.assertRaises(RuntimeError): self.deliver()
        self.assertFalse(self.client.sends)
    def test_quiet_hours_queue_ordinary_instant(self):
        self.ingest(article()); self.deliver(quiet=True)
        self.assertFalse(self.client.sends)
        self.deliver()
        self.assertEqual(len(self.client.sends), 1)
    def test_briefing_not_repeated_without_update(self):
        self.ingest(article()); self.deliver('briefing'); self.deliver('briefing')
        self.assertEqual(len(self.client.sends), 2)
    def test_pending_iso_date_and_live_datetime(self):
        a = article(); a['pub'] = NOW.isoformat()
        self.ingest(a, article(url='https://example.com/2'))
        self.assertEqual(len(self.state['stories']), 1)
    def test_long_and_escaped_titles_fit(self):
        self.ingest(article('<unsafe> & "' * 50, 'https://example.com/' + 'a'*4000))
        result = sm.card(next(iter(self.state['stories'].values())))
        self.assertNotIn('<unsafe>', result)
        self.assertLess(len(result), 3900)
    def test_private_channel_link(self):
        self.assertEqual(sm.message_link(dict(chat_id=-10012345), 7), 'https://t.me/c/12345/7')
    def test_not_modified_is_success(self):
        response = Mock(status_code=400)
        response.json.return_value = dict(ok=False, description='Bad Request: message is not modified')
        with patch.object(sm.requests, 'post', return_value=response):
            self.assertTrue(sm.Telegram('test', 'test').call('editMessageText'))
    def test_briefing_cap_retains_backlog(self):
        for i in range(7):
            self.ingest(article(f'지역사업{i} 소식{i} 발표', f'https://example.com/{i}', 1))
        self.deliver('briefing')
        self.assertEqual(len(self.client.sends), 6)
        self.assertEqual(sum(s['message_id'] is None for s in self.state['stories'].values()), 2)
        self.deliver('briefing')
        self.assertEqual(sum(s['message_id'] is None for s in self.state['stories'].values()), 0)


if __name__ == '__main__':
    unittest.main()
