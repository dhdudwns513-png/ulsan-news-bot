"""Persistent, conservative title-based news threads; no generated factual summaries."""
import copy
import hashlib
import html
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests

TOPICS = {
    "노사": ("임단협", "임금", "교섭", "파업", "노조", "단체협약"),
    "안전": ("사망", "중대재해", "산업재해", "산재", "추락", "화재"),
    "투자": ("투자", "공장", "생산기지", "신설", "증설"),
    "채용": ("채용", "모집", "공채"),
}
STOP = {"울산", "동구", "울산동구", "기자", "뉴스", "발표", "관련", "오늘"}
IMPORTANT = {
    "합의": r"(?:잠정)?합의(?:안|에|를|가|는|\s|$)|타결",
    "사망": r"사망|숨져",
    "파업돌입": r"파업\s*(?:돌입|시작)",
}


def esc(value):
    return html.escape(str(value), quote=True)


def words(title):
    title = re.sub(r"\[[^]]*\]", " ", title)
    return {w.lower() for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", title)} - STOP


def topic(title):
    return {key for key, needles in TOPICS.items() if any(n in title for n in needles)}


def match_score(a, b):
    ta, tb = topic(a), topic(b)
    if ta and tb and not ta & tb:
        return 0
    wa, wb = words(a), words(b)
    common = wa & wb
    # A company name alone is not sufficient to identify an event.
    if len(common) < 2:
        return 0
    return len(common) / max(1, len(wa | wb))


def important(title):
    # A question/proposal is not a completed development.
    if any(w in title for w in ("가능", "전망", "예고", "촉구", "요구", "?", "불발", "결렬")):
        return set()
    return {key for key, pattern in IMPORTANT.items() if re.search(pattern, title)}


def link(url, label):
    if len(url) > 500 or urlsplit(url).scheme not in ("http", "https"):
        return esc(label)
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def card(story):
    articles = sorted(story["articles"], key=lambda a: a["pub"], reverse=True)
    previous = story.get('displayed_count', len(articles) if story.get('message_id') else 0)
    additions = story['articles'][previous:]
    lines = [f'<b>{esc(story["title"][:120])}</b>',
             f'관련 기사 {len(articles)}건 · 갱신 {story["updated"][5:16].replace("T", " ")}',
             '']
    if previous and additions:
        lines += [f'<b>이번에 추가된 보도 · {len(additions)}건</b>']
        for a in sorted(additions, key=lambda a: a['pub'], reverse=True)[:2]:
            lines.append('• ' + esc(a['title'][:140]))
        lines += ['※ 새 기사 제목 기준 · 새로운 사실인지는 원문 확인', '']
    lines.append('<b>최근 보도 제목</b>')
    seen = set()
    for a in articles:
        key = ''.join(sorted(words(a["title"])))
        if key in seen:
            continue
        seen.add(key)
        row = '• ' + esc(a["title"][:140])
        if len('\n'.join(lines)) + len(row) > 2200:
            break
        lines.append(row)
        if len(seen) == 3:
            break
    lines += ['', '<b>관련 기사 원문</b>']
    shown = 0
    for a in articles[:5]:
        row = '• ' + link(a["link"], f'{a.get("source", "원문")[:60]} · {a["pub"][5:16].replace("T", " ")}')
        if len('\n'.join(lines)) + len(row) > 3600:
            break
        lines.append(row)
        shown += 1
    if len(articles) > shown:
        lines.append(f'외 {len(articles)-shown}건 수집 · 최근 원문 {shown}건 표시')
    return '\n'.join(lines)


class Telegram:
    def __init__(self, token, chat):
        if not token or not chat:
            raise RuntimeError('Telegram 설정 누락')
        self.token, self.chat = token, chat

    def call(self, method, **payload):
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/{method}',
                                     json=payload, timeout=15)
            data = response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError(f'Telegram {method} 응답 확인 실패') from None
        if response.status_code == 200 and data.get('ok') is True:
            return data.get('result')
        description = data.get('description', '')
        if method == 'editMessageText' and 'message is not modified' in description:
            return True
        # Deleted cards may be recreated; other errors must remain visible.
        if method == 'editMessageText' and 'message to edit not found' in description:
            return None
        raise RuntimeError(f'Telegram {method} 실패 HTTP {response.status_code}')

    def identity(self):
        bot = self.call('getMe')
        chat = self.call('getChat', chat_id=self.chat)
        print(f'[route] bot=@{bot.get("username", "")} chat={chat.get("title", chat["type"])}')
        return {'bot_id': bot['id'], 'chat_id': chat['id'], 'username': chat.get('username')}

    def send(self, text, silent=True):
        result = self.call('sendMessage', chat_id=self.chat, text=text, parse_mode='HTML',
                           disable_web_page_preview=True, disable_notification=silent)
        time.sleep(1.1)
        return result['message_id']

    def edit(self, message_id, text):
        result = self.call('editMessageText', chat_id=self.chat, message_id=message_id,
                           text=text, parse_mode='HTML', disable_web_page_preview=True)
        time.sleep(1.1)
        return result is not None


def message_link(route, message_id):
    if route.get('username'):
        return f'https://t.me/{route["username"]}/{message_id}'
    chat = str(route['chat_id'])
    if chat.startswith('-100'):
        return f'https://t.me/c/{chat[4:]}/{message_id}'
    return ''


def ingest(state, articles, now, cfg):
    stories = state.setdefault('stories', {})
    known = {a['link'] for s in stories.values() for a in s['articles']}
    def pubtime(a):
        return a["pub"] if isinstance(a["pub"], datetime) else datetime.fromisoformat(a["pub"])
    for article in sorted(articles, key=pubtime):
        if article['link'] in known:
            continue
        a = dict(article)
        a['pub'] = a['pub'].isoformat() if isinstance(a['pub'], datetime) else a['pub']
        candidates = []
        for s in stories.values():
            age = abs((datetime.fromisoformat(a['pub']) - datetime.fromisoformat(s['articles'][0]['pub'])).total_seconds())
            if age > cfg.get('story_window_hours', 72) * 3600:
                continue
            score = match_score(s['title'], a['title'])
            if score >= cfg.get('story_match_threshold', 0.45):
                candidates.append((score, s))
        if candidates:
            s = max(candidates, key=lambda pair: pair[0])[1]
        else:
            sid = hashlib.sha256(a['link'].encode()).hexdigest()[:20]
            s = stories[sid] = {'id': sid, 'title': a['title'], 'articles': [],
                               'message_id': None, 'notified': [], 'priority': 0,
                               'dirty': True, 'briefed': 0}
        s.setdefault('displayed_count', len(s['articles']) if s.get('message_id') else 0)
        s['articles'].append(a)
        s['priority'] = max(s['priority'], a.get('priority', 0))
        s['updated'] = now.isoformat()
        s['dirty'] = True
        known.add(a['link'])


def deliver(state, mode, now, cfg, client, save, quiet=False):
    route = client.identity()
    old_route = state.get('story_route')
    if old_route and any(old_route[k] != route[k] for k in ('bot_id', 'chat_id')):
        raise RuntimeError('봇 또는 채널이 변경됨: 기존 메시지와 연결을 확인하세요')
    state['story_route'] = route
    stories = state.get('stories', {})
    ordered = sorted(stories.values(), key=lambda s: (s['priority'], s['updated']), reverse=True)
    # Existing cards are always edited. New ordinary cards wait for a briefing.
    new_count = 0
    for s in ordered:
        if not s['dirty']:
            continue
        first = not s['message_id']
        if first:
            if mode != 'briefing' and (s['priority'] < 2 or (quiet and s['priority'] < 3)):
                continue
            cap = cfg.get('story_briefing_top', 5) if mode == 'briefing' else cfg.get('check_cap', 12)
            if new_count >= cap:
                continue
        text = card(s)
        if first or not client.edit(s['message_id'], text):
            s['message_id'] = client.send(text, silent=(mode == 'briefing' or s['priority'] < 3))
            new_count += 1
        s['displayed_count'] = len(s['articles'])
        s['dirty'] = False
        s['url'] = message_link(route, s['message_id'])
        # Checkpoint each successful card; retries edit instead of reposting.
        if first:
            s['notified'] = sorted(set().union(*(important(a['title']) for a in s['articles'])))
        save(state)
    # Only newly observed major development markers merit a separate short alert.
    for s in ordered:
        if not s['message_id'] or s['dirty'] or s['priority'] < 2:
            continue
        events = set().union(*(important(a['title']) for a in s['articles']))
        fresh = events - set(s['notified'])
        if not fresh:
            continue
        article = next(a for a in reversed(s['articles']) if important(a['title']) & fresh)
        target = s.get('url') or article['link']
        client.send('<b>주요 후속 보도</b>\n' + esc(article['title'][:180]) + '\n' + link(target, '사안 모아보기'), silent=False)
        s['notified'] = sorted(events)
        save(state)
    if mode == 'briefing':
        changed = [s for s in ordered if s['message_id'] and len(s['articles']) > s['briefed']]
        selected = changed[:cfg.get('story_briefing_top', 5)]
        if selected:
            lines = [f'<b>동구 뉴스 · 업데이트 모아보기</b> {now:%m/%d %H:%M}', '']
            for i, s in enumerate(selected, 1):
                target = s.get('url') or s['articles'][-1]['link']
                label = '신규' if not s['briefed'] else f'+{len(s["articles"])-s["briefed"]}건'
                lines.append(f'{i}. ' + link(target, s['title'][:110]) + f' [{label}]')
            remaining = sum(1 for s in ordered if len(s['articles']) > s['briefed']) - len(selected)
            if remaining:
                lines.append(f'\n추가 {remaining}개 사안은 다음 브리핑 대기')
            client.send('\n'.join(lines), silent=True)
            for s in selected:
                s['briefed'] = len(s['articles'])
            save(state)
    state['last_check'] = now.isoformat()
    save(state)
    return state


def run(bot, cfg, state, mode):
    now = datetime.now(bot.KST)
    since = now - timedelta(hours=max(24, cfg.get('briefing_hours', 12)))
    # Existing sent history prevents migration from flooding the channel.
    sent = set(state.get('sent', []))
    for s in state.get('stories', {}).values():
        sent.update(a['link'] for a in s['articles'])
    articles = {}
    for kind in ('instant', 'instant_title', 'digest'):
        for kw in cfg.get(kind, []):
            for a in bot.fetch_keyword(kw, since, cfg, sent, title_only=(kind != 'instant')):
                if kind != 'instant' and bot.is_market(a, cfg):
                    continue
                priority = 3 if kind == 'instant' else (2 if kind == 'instant_title' else 1)
                old = articles.get(a['link'])
                if not old or priority > old['priority']:
                    articles[a['link']] = {**a, 'priority': priority}
    # Migrate queued articles even though legacy code already marked them sent.
    for p in state.get('pending', []):
        articles.setdefault(p['link'], {**p, 'priority': 2})
    working = copy.deepcopy(state)
    ingest(working, articles.values(), now, cfg)
    working['pending'] = []
    save = lambda value: bot.save_json(bot.STATE_PATH, value)
    return deliver(working, mode, now, cfg, Telegram(bot.TG_TOKEN, bot.TG_CHAT), save, bot.is_quiet(cfg))
