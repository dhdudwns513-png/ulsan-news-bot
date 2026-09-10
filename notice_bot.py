# -*- coding: utf-8 -*-
"""
기관 홈페이지 게시판 알림 모듈 (표준 전자정부 게시판 cop/bbs 형식)
- boards.json 에 정의된 게시판을 읽어 새 글을 텔레그램으로 발송
- notice_state.json 에 이미 보낸 글 ID를 기록해 중복 방지
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
BASE = os.path.dirname(os.path.abspath(__file__))
BOARDS_PATH = os.path.join(BASE, "boards.json")
STATE_PATH = os.path.join(BASE, "notice_state.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
LINK_RE = re.compile(r'href="([^"]*selectBoardArticle\.do[^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
NUM_ONLY_RE = re.compile(r"^[\d\s,]*$")
DATE_RE = re.compile(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})(?!\d)")
TAG_RE = re.compile(r"<[^>]+>")


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clean(text):
    t = html.unescape(TAG_RE.sub(" ", text or ""))
    return re.sub(r"\s+", " ", t).strip()


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def abs_url(base, href):
    href = html.unescape(href).strip()
    if href.startswith("http"):
        return href
    root = re.match(r"(https?://[^/]+)", base).group(1)
    return root + (href if href.startswith("/") else "/" + href)


def pick_title(row):
    """행 안의 여러 링크 중 실제 제목 앵커 선택 (번호 칸 링크 배제)"""
    best = None
    for href, raw in LINK_RE.findall(row):
        t = clean(raw)
        t = re.sub(r"^(새글|NEW|new|신규)\s*", "", t).strip()
        if not t or NUM_ONLY_RE.match(t):
            continue
        if best is None or len(t) > len(best[1]):
            best = (href, t)
    return best


def fetch_html(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    print(f"[board] {url}: {last}", file=sys.stderr)
    return None


def parse_board(url):
    """표준 게시판 목록에서 [{id, title, url, date}] 추출"""
    text = fetch_html(url)
    if text is None:
        return []
    items = []
    for row in ROW_RE.findall(text):
        picked = pick_title(row)
        if not picked:
            continue
        href, title = picked
        if len(title) < 3:
            continue
        link = abs_url(url, href)
        nid = re.search(r"nttId=(\d+)", link)
        if not nid:
            continue
        d = DATE_RE.search(clean(row))
        date = f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else ""
        items.append({"id": nid.group(1), "title": title, "url": link, "date": date})
    return items


def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[tg] 토큰/채팅ID 없음 — 출력만 합니다\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"[tg] {r.status_code} {r.text}", file=sys.stderr)
    time.sleep(0.4)


def main():
    cfg = load_json(BOARDS_PATH, {})
    state = load_json(STATE_PATH, {})
    today = datetime.now(KST).date()
    max_age = cfg.get("max_age_days", 3)
    first_run_limit = cfg.get("first_run_limit", 3)
    exclude = cfg.get("exclude", [])

    for b in cfg.get("boards", []):
        key = b["name"]
        seen = set(state.get(key, []))
        first_run = not seen
        items = parse_board(b["url"])
        if not items:
            continue
        fresh = []
        for it in items:
            if it["id"] in seen:
                continue
            if any(x in it["title"] for x in exclude):
                seen.add(it["id"])
                continue
            if it["date"]:
                try:
                    d = datetime.strptime(it["date"], "%Y-%m-%d").date()
                    if (today - d).days > max_age:
                        seen.add(it["id"])
                        continue
                except ValueError:
                    pass
            fresh.append(it)
        if first_run:
            fresh = fresh[:first_run_limit]
        fresh.reverse()  # 오래된 것부터
        for it in fresh:
            date = it["date"] or today.strftime("%Y-%m-%d")
            tg_send(f'📋 <b>[{esc(b["label"])}]</b>\n'
                    f'<a href="{it["url"]}">{esc(it["title"])}</a>\n'
                    f'   <i>{esc(date)}</i>')
            seen.add(it["id"])
        # 목록에 남은 글은 전부 확인 처리 (오래된 글 재발송 방지)
        for it in items:
            seen.add(it["id"])
        state[key] = list(seen)[-500:]

    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
