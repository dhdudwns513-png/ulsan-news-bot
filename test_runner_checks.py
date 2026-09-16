import base64
import unittest
from unittest.mock import Mock, patch
from test_news_runner import EncryptedState, validate_route

class RunnerTests(unittest.TestCase):
    def test_encrypted_state_roundtrip_and_no_plaintext(self):
        writer = EncryptedState('test/repo', 'github-test', 'telegram-test')
        response = Mock(status_code=201)
        response.json.return_value = {'content': {'sha': 'test-sha'}}
        state = {'story_route': {'chat_id': -100123}, 'sent': ['https://example.com']}
        with patch('test_news_runner.requests.put', return_value=response) as put:
            writer.save(state)
            writer.save(state)
            self.assertEqual(put.call_count, 1)
            encrypted = put.call_args.kwargs['json']['content']
        self.assertNotIn(b'chat_id', base64.b64decode(encrypted))
        fetched = Mock(status_code=200)
        fetched.json.return_value = {'sha': 'test-sha', 'content': encrypted}
        with patch('test_news_runner.requests.get', return_value=fetched):
            self.assertEqual(EncryptedState('test/repo', 'github-test', 'telegram-test').load(), state)
            with self.assertRaises(RuntimeError):
                EncryptedState('test/repo', 'github-test', 'changed-token').load()
    def test_failed_state_write_does_not_advance_sha(self):
        writer = EncryptedState('test/repo', 'github-test', 'telegram-test')
        with patch('test_news_runner.requests.put', return_value=Mock(status_code=409)):
            with self.assertRaises(RuntimeError): writer.save({'sent': []})
        self.assertIsNone(writer.sha)
    def test_wrong_bot_stops(self):
        client = Mock(); client.call.return_value = {'username': 'donggu_news_bot'}
        with self.assertRaises(RuntimeError): validate_route(client)
        self.assertEqual(client.call.call_count, 1)
    def test_missing_post_permission_stops(self):
        client = Mock(); client.call.side_effect = [
            {'username': 'kimtaeseon_news_test_0914_bot', 'id': 1},
            {'type': 'channel', 'title': '김태선 뉴스봇 테스트'},
            {'status': 'administrator', 'can_post_messages': False}]
        with self.assertRaises(RuntimeError): validate_route(client)

if __name__ == '__main__': unittest.main()
