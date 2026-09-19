# -*- coding: utf-8 -*-
"""
기관 홈페이지 게시판 알림 모듈 (표준 전자정부 게시판 cop/bbs 형식)
- boards.json 에 정의된 게시판을 읽어 새 글을 텔레그램으로 발송
- notice_state.json 에 이미 보낸 글 ID를 기록해 중복 방지
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import ssl
from urllib3.util.ssl_ import create_urllib3_context

KST = timezone(timedelta(hours=9))
DEADLINE = [None]  # 전체 실행 마감 시각(초)
BASE = os.path.dirname(os.path.abspath(__file__))
BOARDS_PATH = os.path.join(BASE, "boards.json")
STATE_PATH = os.path.join(BASE, "notice_state.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

class LegacyTLSAdapter(requests.adapters.HTTPAdapter):
    """구형 정부 서버(TLS1.0/약한 암호군)에 접속하기 위한 어댑터"""

    def init_poolmanager(self, *a, **kw):
        ctx = create_urllib3_context(ciphers="DEFAULT@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        for opt in ("OP_NO_SSLv2", "OP_NO_SSLv3"):
            ctx.options |= getattr(ssl, opt, 0)
        try:
            ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        except Exception:
            pass
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def make_session():
    s = requests.Session()
    s.mount("https://", LegacyTLSAdapter())
    return s


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


def stable_id(title, label=""):
    """제목 기반 고정 ID (실행마다 값이 바뀌지 않음)"""
    key = re.sub(r"\s+", "", label + title)
    return "t" + hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


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


def fetch_html(url, tries=1, budget=15):
    """구형 TLS 서버 대응 + http 폴백. 게시판당 시간 예산 제한."""
    import warnings
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    started = time.time()

    attempts = [(make_session(), url)]
    if url.startswith("https://"):
        attempts.append((make_session(), "http://" + url[len("https://"):]))
    attempts.append((requests, url))

    last = None
    for i in range(tries):
        for sess, u in attempts:
            if time.time() - started > budget:
                print(f"[board] {url}: 시간 초과 — 이번 회차 건너뜀", file=sys.stderr)
                return None
            if DEADLINE[0] and time.time() > DEADLINE[0]:
                print(f"[board] {url}: 전체 시간 마감 — 다음 실행에서 확인", file=sys.stderr)
                return None
            try:
                r = sess.get(u, headers=UA, timeout=8, verify=False)
                r.raise_for_status()
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            except Exception as e:
                last = e
        time.sleep(2)
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


EMW_ID_RE = re.compile(r"not_ancmt_mgt_no=(\d+)|goView\D{0,10}(\d+)|fn_view\D{0,10}(\d+)")
EMW_A_RE = re.compile(r"<a\s[^>]*>(.*?)</a>", re.S | re.I)


def parse_eminwon(board):
    """새올(eminwon) 고시공고 목록 파서"""
    text = fetch_html(board["url"])
    if text is None:
        return []
    tmpl = board.get("detail_url", "")
    items = []
    for row in ROW_RE.findall(text):
        m = EMW_ID_RE.search(row)
        if not m:
            continue
        nid = m.group(1) or m.group(2) or m.group(3)
        titles = [clean(t) for t in EMW_A_RE.findall(row)]
        titles = [t for t in titles
                  if t and not NUM_ONLY_RE.match(t) and t not in ("목록", "상세보기", "다운로드")]
        if not titles:
            continue
        title = max(titles, key=len)
        if len(title) < 3:
            continue
        d = DATE_RE.search(clean(row))
        date = f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else ""
        items.append({"id": nid, "title": title,
                      "url": tmpl.replace("{id}", nid) if tmpl else board["url"],
                      "date": date})
    return items


ANY_A_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
ID_IN_HREF_RE = re.compile(r"(?:dataId|dataSid|nttId|not_ancmt_mgt_no|seq|idx|articleNo)=(\d+)")


def parse_generic(board):
    """행 단위로 가장 긴 앵커를 제목으로 삼는 범용 목록 파서"""
    url = board["url"]
    text = fetch_html(url)
    if text is None:
        return []
    skip = ("목록", "상세보기", "다운로드", "미리보기", "이전", "다음", "처음", "마지막", "검색")
    items, used = [], set()
    for row in ROW_RE.findall(text):
        best = None
        for href, raw in ANY_A_RE.findall(row):
            t = re.sub(r"^(새글|NEW|new|신규)\s*", "", clean(raw)).strip()
            t = re.sub(r"\s*(새글|NEW)$", "", t).strip()
            if not t or NUM_ONLY_RE.match(t) or t in skip:
                continue
            if best is None or len(t) > len(best[1]):
                best = (href, t)
        if not best or len(best[1]) < 5:
            continue
        href, title = best
        m = ID_IN_HREF_RE.search(html.unescape(href))
        nid = m.group(1) if m else stable_id(title, board.get("label", ""))
        if nid in used:
            continue
        used.add(nid)
        link = abs_url(url, href) if href.lower().startswith(("http", "/")) else url
        d = DATE_RE.search(clean(row))
        date = f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else ""
        items.append({"id": nid, "title": title, "url": link, "date": date})
    return items


def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        raise RuntimeError("[tg] 토큰/채팅ID 없음 — 발송 기록 미저장")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        ok = r.status_code == 200 and r.json().get("ok") is True
    except (requests.RequestException, ValueError):
        raise RuntimeError("[tg] 전송 응답 확인 실패 — 발송 기록 미저장") from None
    if not ok:
        raise RuntimeError(f"[tg] 전송 거절 HTTP {r.status_code} — 발송 기록 미저장")
    time.sleep(0.4)


NARA_ID_RE = re.compile(r"fn_apmView\(\s*'(\d+)'\s*,\s*'(\d+)'\s*\)")


def parse_narailteo(board):
    """나라일터 모집공고 — 여러 페이지를 읽어 지역 키워드로 필터"""
    base = board["url"]
    pages = board.get("pages", 5)
    need = board.get("match_any", ["울산"])
    items, seen_ids = [], set()
    for p in range(1, pages + 1):
        sep = "&" if "?" in base else "?"
        text = fetch_html(f"{base}{sep}pageIndex={p}")
        if text is None:
            break
        rows = ROW_RE.findall(text)
        if not rows:
            break
        for row in rows:
            m = NARA_ID_RE.search(row)
            if not m:
                continue
            nid = m.group(2)
            if nid in seen_ids:
                continue
            cells = [clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
            picked = pick_any_title(row)
            if not picked:
                continue
            title = picked
            org = ""
            for c in cells:
                if c and c != title and ("청" in c or "시" in c or "군" in c or "구" in c
                                         or "공단" in c or "공사" in c or "재단" in c
                                         or "원" in c or "센터" in c):
                    if len(c) < 60 and not DATE_RE.search(c):
                        org = c
                        break
            blob = title + " " + org
            if not any(w in blob for w in need):
                continue
            d = DATE_RE.search(" ".join(cells))
            date = f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else ""
            seen_ids.add(nid)
            label_title = f"{org} · {title}" if org else title
            items.append({"id": "n" + nid, "title": label_title,
                          "url": board.get("detail_url", base).replace("{id}", nid),
                          "date": date})
    return items


def pick_any_title(row):
    """행에서 가장 긴 링크 텍스트를 제목으로"""
    best = ""
    for _, raw in ANY_A_RE.findall(row):
        t = clean(raw)
        if len(t) > len(best):
            best = t
    if not best:
        for raw in EMW_A_RE.findall(row):
            t = clean(raw)
            if len(t) > len(best):
                best = t
    return best if len(best) >= 5 else ""


def tg_send_long(text):
    """4096자 제한에 맞춰 줄 단위로 나눠 발송"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        tg_send(c)


def main():
    cfg = load_json(BOARDS_PATH, {})
    state = load_json(STATE_PATH, {})
    today = datetime.now(KST).date()
    max_age = cfg.get("max_age_days", 3)
    first_run_limit = cfg.get("first_run_limit", 3)
    max_per_board = cfg.get("max_per_board", 10)
    exclude = cfg.get("exclude", [])
    DEADLINE[0] = time.time() + cfg.get("total_budget_sec", 210)

    # 매 실행마다 시작 게시판을 한 칸씩 밀어 모든 게시판이 돌아가며 확인되도록
    all_boards = cfg.get("boards", [])
    start = state.get("_rotate", 0) % max(len(all_boards), 1)
    ordered = all_boards[start:] + all_boards[:start]
    state["_rotate"] = start + cfg.get("rotate_step", 5)

    collected = []  # [(label, [item,...])]

    for b in ordered:
        if time.time() > DEADLINE[0]:
            print("[board] 전체 시간 마감 — 나머지 게시판은 다음 실행", file=sys.stderr)
            break
        key = b["name"]
        seen = set(state.get(key, []))
        first_run = not seen
        btype = b.get("type", "cop")
        if btype == "eminwon":
            items = parse_eminwon(b)
        elif btype == "narailteo":
            items = parse_narailteo(b)
        elif btype == "generic":
            items = parse_generic(b)
        else:
            items = parse_board(b["url"])
        if not items:
            continue

        fresh = []
        for it in items:
            tkey = stable_id(it["title"], key)
            if it["id"] in seen or tkey in seen:
                continue
            need = b.get("include_only", [])
            if need and not any(w in it["title"] for w in need):
                seen.add(it["id"])
                seen.add(tkey)
                continue
            if any(x in it["title"] for x in exclude):
                seen.add(it["id"])
                seen.add(tkey)
                continue
            if it["date"]:
                try:
                    d = datetime.strptime(it["date"], "%Y-%m-%d").date()
                    if (today - d).days > max_age:
                        seen.add(it["id"])
                        seen.add(tkey)
                        continue
                except ValueError:
                    pass
            it["_tkey"] = tkey
            fresh.append(it)

        if first_run:
            fresh = fresh[:first_run_limit]
        fresh = fresh[:max_per_board]
        fresh.reverse()  # 오래된 것부터

        if fresh:
            collected.append((b["label"], fresh))

        # 보낸 글 + 목록에 있던 글 전부 확인 처리 (중복 방지)
        for it in fresh:
            seen.add(it["id"])
            seen.add(it.get("_tkey", ""))
        for it in items:
            seen.add(it["id"])
            seen.add(stable_id(it["title"], key))
        seen.discard("")
        state[key] = list(seen)[-1000:]

    # 기관·게시판별로 한 건의 메시지로 묶어 발송
    if collected:
        now = datetime.now(KST).strftime("%m/%d %H:%M")
        total = sum(len(v) for _, v in collected)
        lines = [f"📋 <b>기관 공지·고시 알림</b>  {now}",
                 f"<i>신규 {total}건</i>", ""]
        for label, items in collected:
            lines.append(f"<b>▎{esc(label)}</b>")
            for it in items:
                date = it["date"] or today.strftime("%Y-%m-%d")
                lines.append(f'• <a href="{it["url"]}">{esc(it["title"])}</a>')
                lines.append(f'   <i>{esc(date)}</i>')
            lines.append("")
        tg_send_long("\n".join(lines))

    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
