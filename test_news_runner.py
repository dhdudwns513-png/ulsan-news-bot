"""Isolated test-channel runner with encrypted durable delivery state."""
import base64
import hashlib
import json
import os
import sys

import requests
from cryptography.fernet import Fernet, InvalidToken

import news_bot as bot
import story_mode
import comparison

EXPECTED_BOT = 'kimtaeseon_news_test_0914_bot'
EXPECTED_CHAT_TITLE = '김태선 뉴스봇 테스트'
STATE_BRANCH = 'test-news-state'
STATE_FILE = 'test-news-state.enc'


class EncryptedState:
    def __init__(self, repository, github_token, telegram_token):
        self.url = f'https://api.github.com/repos/{repository}/contents/{STATE_FILE}'
        self.headers = {'Authorization': f'Bearer {github_token}', 'Accept': 'application/vnd.github+json'}
        key = base64.urlsafe_b64encode(hashlib.sha256(telegram_token.encode()).digest())
        self.cipher = Fernet(key)
        self.sha = None
        self.previous = None

    def load(self):
        try:
            response = requests.get(self.url, headers=self.headers, params={'ref': STATE_BRANCH}, timeout=20)
        except requests.RequestException:
            raise RuntimeError('발송 기록 조회 실패') from None
        if response.status_code == 404:
            return {'sent': [], 'pending': []}
        if response.status_code != 200:
            raise RuntimeError(f'발송 기록 조회 HTTP {response.status_code}')
        data = response.json()
        self.sha = data['sha']
        try:
            decoded = self.cipher.decrypt(base64.b64decode(data['content']))
            state = json.loads(decoded)
        except (InvalidToken, ValueError):
            raise RuntimeError('기존 발송 기록 복호화 실패: 토큰 변경 여부 확인 필요') from None
        self.previous = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
        return state

    def save(self, state):
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
        if raw == self.previous:
            return
        payload = {'message': 'Update encrypted test news delivery state [skip ci]',
                   'branch': STATE_BRANCH,
                   'content': base64.b64encode(self.cipher.encrypt(raw)).decode()}
        if self.sha:
            payload['sha'] = self.sha
        try:
            response = requests.put(self.url, headers=self.headers, json=payload, timeout=20)
        except requests.RequestException:
            raise RuntimeError('발송 기록 저장 실패 — 실행 중단') from None
        if response.status_code not in (200, 201):
            raise RuntimeError(f'발송 기록 저장 실패 HTTP {response.status_code}')
        self.sha = response.json()['content']['sha']
        self.previous = raw


def validate_route(client):
    me = client.call('getMe')
    if me.get('username', '').lower() != EXPECTED_BOT:
        raise RuntimeError('테스트봇 계정 불일치 — 전송 중단')
    chat = client.call('getChat', chat_id=client.chat)
    if chat.get('type') != 'channel' or chat.get('title') != EXPECTED_CHAT_TITLE:
        raise RuntimeError('테스트 채널 불일치 — 전송 중단')
    member = client.call('getChatMember', chat_id=client.chat, user_id=me['id'])
    if member.get('status') != 'administrator' or not member.get('can_post_messages'):
        raise RuntimeError('테스트봇의 채널 게시 권한이 없습니다')
    print('TEST_ROUTE_AND_POST_PERMISSION_VERIFIED')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'check'
    if mode not in ('check', 'briefing'):
        raise ValueError('지원하지 않는 실행 모드')
    client = story_mode.Telegram(bot.TG_TOKEN, bot.TG_CHAT)
    validate_route(client)
    repository = os.environ['GITHUB_REPOSITORY']
    state = EncryptedState(repository, os.environ['STATE_GITHUB_TOKEN'], bot.TG_TOKEN)
    cfg = bot.load_json(bot.CONFIG_PATH, {})
    cfg.update(story_mode=True, story_briefing_top=5, check_cap=3)
    bot.save_json = lambda path, value: state.save(value)
    loaded = state.load()
    if not loaded.get('edit_smoke_verified'):
        message_id = loaded.get('edit_smoke_message_id')
        if not message_id:
            message_id = client.send('기능 점검 · 기사 묶음 메시지 갱신을 확인하고 있습니다.\n실제 뉴스가 아닌 테스트 메시지입니다.', silent=True)
            loaded['edit_smoke_message_id'] = message_id
            state.save(loaded)
        if not client.edit(message_id, '기능 점검 완료 · 기존 메시지 갱신에 성공했습니다.\n후속 기사는 사안별 묶음에 반영됩니다.\n실제 뉴스가 아닌 테스트 메시지입니다.'):
            raise RuntimeError('점검 메시지를 찾을 수 없습니다')
        loaded['edit_smoke_verified'] = True
        state.save(loaded)
        print('LIVE_MESSAGE_EDIT_VERIFIED message_id=' + str(message_id))
    counts = {'test_send_success': 0, 'test_edit_success': 0}
    original_call = story_mode.Telegram.call
    def counted_call(self, method, **payload):
        result = original_call(self, method, **payload)
        if method == 'sendMessage' and result:
            counts['test_send_success'] += 1
        elif method == 'editMessageText' and result:
            counts['test_edit_success'] += 1
        return result
    story_mode.Telegram.call = counted_call
    try:
        result = story_mode.run(bot, cfg, loaded, mode)
    finally:
        story_mode.Telegram.call = original_call
    comparison.collect(result, repository, state.headers, mode, counts)
    state.save(result)
    print('TEST_NEWS_RUN_COMPLETE mode=' + mode)


if __name__ == '__main__':
    main()
