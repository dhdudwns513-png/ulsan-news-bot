# -*- coding: utf-8 -*-
"""
울산 동구 지역 뉴스 텔레그램 봇
- 네이버 뉴스 검색 API + (선택) RSS 피드에서 키워드별 기사 수집
- briefing 모드: 키워드별 묶음 브리핑 발송
- urgent   모드: 긴급 키워드(의원 이름 등) 신규 기사 즉시 발송
- state.json 에 이미 보낸 기사 링크를 기록해 중복 발송 방지
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

KST = timezone(timedelta(hours=9))
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "keywords.json")
STATE_PATH = os.path.join(BASE, "state.json")

NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------- 유틸 ----------
def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def norm_title(title):
    """중복 판정용 제목 정규화 (기호·공백·언론사 꼬리표 제거)"""
    t = re.sub(r"\[.*?\]|\(.*?\)|【.*?】", "", title)
    t = re.sub(r"[^가-힣a-zA-Z0-9]", "", t)
    return t.lower()


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- 수집 ----------
def naver_search(query, display=30):
    """NAVER API HUB (네이버클라우드) 방식 — 키가 있을 때만 동작, 없으면 건너뜀"""
    if not (NAVER_ID and NAVER_SECRET):
        return []
    try:
        r = requests.get(
            "https://naverapihub.apigw.ntruss.com/search/v1/news",
            headers={"X-NCP-APIGW-API-KEY-ID": NAVER_ID, "X-NCP-APIGW-API-KEY": NAVER_SECRET},
            params={"query": query, "display": display, "sort": "date"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[naver] {query}: {e}", file=sys.stderr)
        return []
    out = []
    for it in r.json().get("items", []):
        try:
            pub = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
        except Exception:
            pub = datetime.now(KST)
        link = it.get("originallink") or it.get("link")
        out.append({
            "title": clean(it["title"]),
            "link": link,
            "desc": clean(it.get("description", "")),
            "pub": pub,
            "source": press_from_url(link),
        })
    return out


def google_news_search(query):
    """구글 뉴스 검색 RSS — 키 불필요, 기본 수집원"""
    from urllib.parse import quote
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    feed = {"name": "google", "url": url}
    arts = rss_fetch(feed)
    for a in arts:
        # 구글뉴스 제목은 "기사제목 - 언론사" 형태
        if " - " in a["title"]:
            t, _, press = a["title"].rpartition(" - ")
            a["title"], a["source"] = t.strip(), press.strip()
    return arts


def press_from_url(url):
    m = re.search(r"https?://(?:www\.|news\.|m\.)?([^/]+)", url or "")
    return m.group(1) if m else ""


def rss_fetch(feed):
    """feed: {"name": "...", "url": "..."}"""
    try:
        import feedparser
    except ImportError:
        return []
    try:
        d = feedparser.parse(feed["url"], agent="Mozilla/5.0 (news-bot)")
    except Exception as e:
        print(f"[rss] {feed['name']}: {e}", file=sys.stderr)
        return []
    out = []
    for e in d.entries[:50]:
        pub = datetime.now(KST)
        if getattr(e, "published_parsed", None):
            pub = datetime.fromtimestamp(time.mktime(e.published_parsed), KST)
        out.append({
            "title": clean(e.get("title", "")),
            "link": e.get("link", ""),
            "desc": clean(e.get("summary", "")),
            "pub": pub,
            "source": getattr(getattr(e, "source", None), "title", None) or feed["name"],
        })
    return out


def matches(article, keyword):
    hay = (article["title"] + " " + article["desc"]).replace(" ", "")
    return keyword.replace(" ", "") in hay


def collect(cfg, since):
    """키워드별 신규 기사 dict 반환 {keyword: [article,...]}"""
    result = {}
    seen_titles = set()
    keywords = cfg["keywords"]
    rss_articles = []
    for feed in cfg.get("rss_feeds", []):
        rss_articles += rss_fetch(feed)

    for kw in keywords:
        arts = google_news_search(kw) + naver_search(kw)
        arts += [a for a in rss_articles if matches(a, kw)]
        picked = []
        for a in arts:
            if a["pub"] < since:
                continue
            if any(x in a["title"] for x in cfg.get("exclude", [])):
                continue
            nt = norm_title(a["title"])
            if not nt or nt in seen_titles:
                continue
            seen_titles.add(nt)
            picked.append(a)
        picked.sort(key=lambda x: x["pub"], reverse=True)
        result[kw] = picked
    return result


# ---------- 텔레그램 ----------
def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[tg] 토큰/채팅ID 없음 — 출력만 합니다\n" + text)
        return
    # 4096자 제한 → 줄 단위로 분할
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": c, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[tg] {r.status_code} {r.text}", file=sys.stderr)
        time.sleep(0.5)


def fmt_article(a):
    t = a["pub"].strftime("%m/%d %H:%M")
    return f'• <a href="{a["link"]}">{esc(a["title"])}</a>\n   <i>{esc(a["source"])} · {t}</i>'


# ---------- 모드 ----------
def run_briefing(cfg, state):
    hours = cfg.get("briefing_hours", 24)
    since = datetime.now(KST) - timedelta(hours=hours)
    data = collect(cfg, since)
    sent = set(state.get("sent", []))
    limit = cfg.get("max_per_keyword", 8)

    today = datetime.now(KST).strftime("%Y.%m.%d (%a)")
    lines = [f"📰 <b>울산 동구 뉴스 브리핑</b>  {today}", ""]
    total = 0
    for kw, arts in data.items():
        new = [a for a in arts if a["link"] not in sent][:limit]
        if not new:
            continue
        lines.append(f"<b>▎{esc(kw)}</b>")
        for a in new:
            lines.append(fmt_article(a))
            sent.add(a["link"])
            total += 1
        lines.append("")
    if total == 0:
        lines.append("최근 신규 기사가 없습니다.")
    tg_send("\n".join(lines))
    state["sent"] = list(sent)[-3000:]
    return state


def run_urgent(cfg, state):
    urgent_kw = cfg.get("urgent_keywords", [])
    if not urgent_kw:
        return state
    since = datetime.now(KST) - timedelta(hours=6)
    sub = dict(cfg)
    sub["keywords"] = urgent_kw
    data = collect(sub, since)
    sent = set(state.get("sent", []))
    lines = []
    for kw, arts in data.items():
        for a in arts:
            if a["link"] in sent:
                continue
            lines.append(f"🔔 <b>[{esc(kw)}]</b> 신규 기사\n{fmt_article(a)}")
            sent.add(a["link"])
    if lines:
        tg_send("\n\n".join(lines))
    state["sent"] = list(sent)[-3000:]
    return state


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "briefing").strip()
    cfg = load_json(CONFIG_PATH, {"keywords": []})
    state = load_json(STATE_PATH, {"sent": []})
    if mode == "urgent":
        state = run_urgent(cfg, state)
    else:
        state = run_briefing(cfg, state)
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
