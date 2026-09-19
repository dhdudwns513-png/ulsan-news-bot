import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import news_bot
import notice_bot


class DeliveryTests(unittest.TestCase):
    def test_rejected_delivery_is_an_error(self):
        for module in (news_bot, notice_bot):
            for status, body in ((403, {"ok": False}), (200, {"ok": False})):
                with self.subTest(module=module.__name__, status=status):
                    response = Mock(status_code=status)
                    response.json.return_value = body
                    with patch.object(module, "TG_TOKEN", "test"), patch.object(module, "TG_CHAT", "test"), patch.object(module.requests, "post", return_value=response):
                        with self.assertRaises(RuntimeError):
                            module.tg_send("message")

    def test_successful_delivery(self):
        for module in (news_bot, notice_bot):
            response = Mock(status_code=200)
            response.json.return_value = {"ok": True}
            with patch.object(module, "TG_TOKEN", "test"), patch.object(module, "TG_CHAT", "test"), patch.object(module.requests, "post", return_value=response), patch.object(module.time, "sleep"):
                module.tg_send("message")

    def test_missing_credentials_fail(self):
        for module in (news_bot, notice_bot):
            with patch.object(module, "TG_TOKEN", ""):
                with self.assertRaises(RuntimeError):
                    module.tg_send("message")

    def test_failed_briefing_preserves_saved_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            original = '{"sent": ["previous"], "pending": []}'
            state_path.write_text(original)
            with patch.object(news_bot, "STATE_PATH", str(state_path)), patch.object(news_bot, "CONFIG_PATH", str(Path(tmp) / "missing.json")), patch.object(news_bot.sys, "argv", ["news_bot.py", "briefing"]), patch.object(news_bot, "tg_send", side_effect=RuntimeError("rejected")):
                with self.assertRaises(RuntimeError):
                    news_bot.main()
            self.assertEqual(state_path.read_text(), original)

    def test_search_failure_is_not_empty_news(self):
        with patch.object(news_bot, "NAVER_ID", "test"), patch.object(news_bot, "NAVER_SECRET", "test"), patch.object(news_bot.requests, "get", side_effect=requests.Timeout("timeout")):
            with self.assertRaises(RuntimeError):
                news_bot.naver_search("울산")

    def test_transport_error_does_not_expose_token(self):
        for module in (news_bot, notice_bot):
            with patch.object(module, "TG_TOKEN", "private-token"), patch.object(module, "TG_CHAT", "test"), patch.object(module.requests, "post", side_effect=requests.Timeout("private-token")):
                with self.assertRaises(RuntimeError) as error:
                    module.tg_send("message")
                self.assertNotIn("private-token", str(error.exception))


if __name__ == "__main__":
    unittest.main()
