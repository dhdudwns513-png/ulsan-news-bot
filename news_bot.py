# -*- coding: utf-8 -*-
"""
울산 동구 지역 뉴스 텔레그램 봇 v2
- 3등급 키워드: instant(즉시) / instant_title(제목 포함 시 즉시) / digest(브리핑만, 보도량 top N)
- check 모드: 20분/1시간마다 신규 기사 확인 → 즉시 등급 발송, 나머지는 브리핑 대기
- briefing 모드: 아침·저녁 묶음 발송
- 유사 제목 클러스터링으로 같은 사안 묶기 (대표 1건 + 외 N건)
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

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


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tokens(title):
    """클러스터링용 토큰: 한글 2글자 이상 덩어리·영문·숫자"""
    t = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|「.*?」", " ", title)
    toks = re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}|\d{2,}", t)
    # 한글 덩어리는 앞 2글자 어간도 포함해 조사 변화 흡수
    out = set()
    for w in toks:
        out.add(w)
        if re.match(r"[가-힣]", w) and len(w) > 2:
            out.add(w[:2])
    return out


def similar(a, b, th=0.22):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= th


def cluster(articles):
    """유사 제목끼리 묶어 [{"lead": art, "others": [art...]}] 반환 (보도량 순)"""
    groups = []
    for a in articles:
        for g in groups:
            if any(similar(a["title"], x["title"]) for x in [g["lead"]] + g["others"]):
                g["others"].append(a)
                break
        else:
            groups.append({"lead": a, "others": []})
    for g in groups:
        # 대표 기사는 가장 최신 것으로
        allp = [g["lead"]] + g["others"]
        allp.sort(key=lambda x: x["pub"], reverse=True)
        g["lead"], g["others"] = allp[0], allp[1:]
    groups.sort(key=lambda g: (len(g["others"]), g["lead"]["pub"]), reverse=True)
    return groups


BREAKING_RE = re.compile(r"[\[\(<【]\s*(속보|단독|긴급|1보|특종)\s*[\]\)>】]|^\s*(속보|단독|긴급|1보|특종)\s*[\]\)>】:：\-–]")


def is_breaking(article):
    """제목에 속보/단독 등 표지가 있는지"""
    head = article["title"][:30]
    return bool(BREAKING_RE.search(head))


def is_market(article, cfg):
    """주가·지분·수급 등 증권성 기사 판별"""
    text = article["title"] + " " + article.get("desc", "")
    return any(w in text for w in cfg.get("market_words", []))


def region_ok(article, cfg):
    """타지역 기사 배제 — 울산이 함께 언급되면 통과"""
    text = article["title"] + " " + article.get("desc", "")
    if "울산" in text:
        return True
    return not any(bad in text for bad in cfg.get("region_block", []))


def require_ok(article, cfg, kw):
    """키워드별 필수 동반어 검사 (예: '동구청'은 '울산'이 함께 있어야 함)"""
    need = cfg.get("keyword_require", {}).get(kw, [])
    if not need:
        return True
    text = (article["title"] + " " + article.get("desc", "")).replace(" ", "")
    return all(w.replace(" ", "") in text for w in need)


def press_from_url(url):
    m = re.search(r"https?://(?:www\.|news\.|m\.|v\.)?([^/]+)", url or "")
    return m.group(1) if m else ""


# ---------- 수집 ----------
def rss_fetch(feed):
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
    for e in d.entries[:100]:
        pub = datetime.now(KST)
        if getattr(e, "published_parsed", None):
            pub = datetime.fromtimestamp(time.mktime(e.published_parsed), KST)
        src = getattr(getattr(e, "source", None), "title", None)
        out.append({
            "title": clean(e.get("title", "")),
            "link": e.get("link", ""),
            "desc": clean(e.get("summary", "")),
            "pub": pub,
            "source": src or feed["name"],
        })
    return out


def google_news_search(query):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    arts = rss_fetch({"name": "", "url": url})
    for a in arts:
        if " - " in a["title"]:
            t, _, press = a["title"].rpartition(" - ")
            a["title"] = t.strip()
            if not a["source"] or "." in a["source"]:
                a["source"] = press.strip()
        if not a["source"]:
            a["source"] = press_from_url(a["link"])
    return arts


def naver_search(query, display=50):
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
        out.append({"title": clean(it["title"]), "link": link,
                    "desc": clean(it.get("description", "")),
                    "pub": pub, "source": press_from_url(link)})
    return out


def fetch_keyword(kw, since, cfg, sent, title_only=False):
    arts = []
    if cfg.get("use_google", True):
        arts += google_news_search(kw)
    if cfg.get("use_naver", True):
        arts += naver_search(kw)
    picked, seen = [], set()
    kws = kw.replace(" ", "")
    for a in arts:
        if a["pub"] < since or a["link"] in sent:
            continue
        if any(x in a["title"] for x in cfg.get("exclude", [])):
            continue
        if not region_ok(a, cfg):
            continue
        if not require_ok(a, cfg, kw):
            continue
        hay = a["title"].replace(" ", "") if title_only else (a["title"] + a["desc"]).replace(" ", "")
        if kws not in hay:
            continue
        if a["link"] in seen:
            continue
        seen.add(a["link"])
        picked.append(a)
    return picked


# ---------- 텔레그램 ----------
def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[tg] 토큰/채팅ID 없음 — 출력만 합니다\n" + text)
        return
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


def fmt_group(g):
    a = g["lead"]
    t = a["pub"].strftime("%m/%d %H:%M")
    line = f'• <a href="{a["link"]}">{esc(a["title"])}</a>\n   <i>{esc(a["source"])} · {t}'
    if g["others"]:
        line += f" · 외 {len(g['others'])}건"
    line += "</i>"
    return line


def is_quiet(cfg):
    h = datetime.now(KST).hour
    qs, qe = cfg.get("quiet_hours", [23, 7])
    return h >= qs or h < qe


# ---------- 모드 ----------
def run_check(cfg, state):
    """즉시 등급 확인. 속보/단독은 등급·시간·상한 무시하고 개별 발송."""
    sent = set(state.get("sent", []))
    since = datetime.now(KST) - timedelta(hours=6)
    quiet = is_quiet(cfg)
    cap = cfg.get("check_cap", 5)
    msgs = []
    pending = state.get("pending", [])

    # 0) 속보·단독 우선 처리 (전 등급 대상, 개별 발송)
    for kw in cfg.get("breaking_watch", []):
        for a in fetch_keyword(kw, since, cfg, sent):
            if not is_breaking(a):
                continue
            t = a["pub"].strftime("%m/%d %H:%M")
            msgs.append(f'🚨 <b>[{esc(kw)} · 속보]</b>\n'
                        f'<a href="{a["link"]}">{esc(a["title"])}</a>\n'
                        f'   <i>{esc(a["source"])} · {t}</i>')
            sent.add(a["link"])

    # 1) 🔴 즉시 등급 — 묶지 않고 기사 하나하나 개별 발송
    for kw in cfg.get("instant", []):
        arts = fetch_keyword(kw, since, cfg, sent)
        arts.sort(key=lambda x: x["pub"])
        for a in arts:
            t = a["pub"].strftime("%m/%d %H:%M")
            msgs.append(f'🔴 <b>[{esc(kw)}]</b>\n'
                        f'<a href="{a["link"]}">{esc(a["title"])}</a>\n'
                        f'   <i>{esc(a["source"])} · {t}</i>')
            sent.add(a["link"])

    # 2) 🟡 제목 포함 시 즉시
    n = 0
    for kw in cfg.get("instant_title", []):
        found = fetch_keyword(kw, since, cfg, sent, title_only=True)
        # 증권성 기사는 즉시 알림 대신 브리핑으로
        # 증권성 기사는 알림·브리핑 모두에서 제외
        for a in [x for x in found if is_market(x, cfg)]:
            sent.add(a["link"])
        found = [a for a in found if not is_market(a, cfg)]
        for g in cluster(found):
            allp = [g["lead"]] + g["others"]
            if quiet or n >= cap:
                for a in allp:
                    if a["link"] not in sent:
                        pending.append({**a, "pub": a["pub"].isoformat(), "kw": kw})
                        sent.add(a["link"])
                continue
            msgs.append(f"🟡 <b>[{esc(kw)}]</b>\n{fmt_group(g)}")
            for a in allp:
                sent.add(a["link"])
            n += 1

    if msgs:
        tg_send("\n\n".join(msgs))
    elif cfg.get("heartbeat"):
        now = datetime.now(KST).strftime("%H:%M")
        tg_send(f"✅ <i>{now} 확인 완료 — 신규 0건</i>")
    state["sent"] = list(sent)[-5000:]
    state["pending"] = pending[-200:]
    state["last_check"] = datetime.now(KST).isoformat()
    return state


def run_briefing(cfg, state):
    sent = set(state.get("sent", []))
    hours = cfg.get("briefing_hours", 12)
    since = datetime.now(KST) - timedelta(hours=hours)
    now = datetime.now(KST)
    label = "아침" if now.hour < 12 else "저녁"
    lines = [f"📰 <b>울산 동구 {label} 브리핑</b>  {now.strftime('%m/%d (%a) %H:%M')}"]
    lc = state.get("last_check")
    if lc:
        lines.append(f"<i>마지막 확인 {datetime.fromisoformat(lc).strftime('%m/%d %H:%M')}</i>")
    lines.append("")
    total = 0

    # 1) 즉시 등급에서 브리핑으로 넘어온 것 (밤사이·상한 초과분)
    pending = state.get("pending", [])
    if pending:
        by_kw = {}
        for p in pending:
            a = dict(p)
            a["pub"] = datetime.fromisoformat(a["pub"])
            by_kw.setdefault(a.pop("kw"), []).append(a)
        for kw, arts in by_kw.items():
            lines.append(f"<b>▎{esc(kw)}</b> <i>(모아둔 기사)</i>")
            for g in cluster(arts)[:cfg.get("max_per_keyword", 8)]:
                lines.append(fmt_group(g))
                total += 1
            lines.append("")
        state["pending"] = []

    # 2) 즉시 등급 키워드의 본문 언급 기사 (제목엔 없어서 즉시 못 간 것)
    for kw in cfg.get("instant_title", []):
        # 일부 키워드는 브리핑에서도 제목 매치만 인정 (본문 스침 방지)
        t_only = kw in cfg.get("briefing_title_only", [])
        arts = [a for a in fetch_keyword(kw, since, cfg, sent, title_only=t_only)
                if not is_market(a, cfg)]
        groups = cluster(arts)[:cfg.get("max_per_keyword", 8)]
        if not groups:
            continue
        lines.append(f"<b>▎{esc(kw)}</b>")
        for g in groups:
            lines.append(fmt_group(g))
            for a in [g["lead"]] + g["others"]:
                sent.add(a["link"])
            total += 1
        lines.append("")

    # 3) digest 키워드: 보도량 top N
    for kw in cfg.get("digest", []):
        title_only = cfg.get("digest_title_only", True)
        arts = [a for a in fetch_keyword(kw, since, cfg, sent, title_only=title_only)
                if not is_market(a, cfg)]
        groups = cluster(arts)[:cfg.get("digest_top", 10)]
        if not groups:
            continue
        lines.append(f"<b>▎{esc(kw)} 주요 뉴스 TOP {len(groups)}</b> <i>(보도량 순)</i>")
        for i, g in enumerate(groups, 1):
            lines.append(f"{i}. " + fmt_group(g)[2:])
            for a in [g["lead"]] + g["others"]:
                sent.add(a["link"])
            total += 1
        lines.append("")

    if total == 0:
        lines.append("신규 기사가 없습니다.")
    tg_send("\n".join(lines))
    state["sent"] = list(sent)[-5000:]
    return state


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").strip()
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"sent": [], "pending": []})
    state = run_briefing(cfg, state) if mode == "briefing" else run_check(cfg, state)
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
