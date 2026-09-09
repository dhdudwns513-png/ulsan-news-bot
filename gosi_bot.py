# -*- coding: utf-8 -*-
"""
울산 동구청 / 울산광역시 고시공고 모니터링 봇  v4
- 게시판이 iframe 안에 있는 경우 자동으로 따라 들어감
- 외부 사이트 링크(페이스북 등) 제외
- DIAG=true 로 실행하면 어떤 주소를 몇 건으로 읽었는지 전부 보여줌
"""

import os
import re
import json
import hashlib
import time
import warnings
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import ssl
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.ssl_ import create_urllib3_context
except Exception:
    create_urllib3_context = None

warnings.filterwarnings("ignore")
try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

KST = timezone(timedelta(hours=9))
STATE_FILE = "state_gosi.json"
DIAG = os.environ.get("DIAG", "false").strip().lower() == "true"


def _env(*names):
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


TOKEN = _env("TELEGRAM_TOKEN", "BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_TOKEN")
CHAT_ID = _env("TELEGRAM_CHAT_ID", "CHAT_ID", "TG_CHAT_ID")

# ---------------------------------------------------------------- 감시 대상
SOURCES = [
    {
        "name": "울산 동구청",
        "tag": "donggu",
        "urls": [
            "https://www.donggu.ulsan.kr/donggu/dongguNews/gosi/contents.do",
            "https://www.donggu.ulsan.kr/cop/bbs/selectSaeolGosiList.do",
            "https://www.donggu.ulsan.kr/cop/bbs/selectGosiList.do",
        ],
        "filter": "all",
    },
    {
        "name": "울산광역시",
        "tag": "ulsan",
        "urls": [
            "https://www.ulsan.go.kr/u/rep/contents.ulsan?mId=001004002000000000",
        ],
        "filter": "donggu_only",
    },
]

# 주민 현안·민원으로 번질 수 있는 공고
HOT = [
    "도시계획", "지구단위", "정비구역", "재개발", "재건축", "실시계획",
    "도시관리계획", "보상", "수용", "공청회", "주민설명회", "열람",
    "개발행위", "철거", "폐쇄", "폐지", "이전", "입법예고", "조례",
    "용도지역", "지정", "해제", "매각", "환경영향", "안전진단",
    "재난", "붕괴", "침수", "악취", "소음",
]

# 사업·예산 관련
WARM = ["공모", "모집", "지원사업", "보조금", "선정", "설명회", "간담회"]

# 울산시 공고 중 동구 관련만 거르는 키워드 (법정동·주요 지명 기준)
DONGGU_KEYS = [
    "동구", "동울산",
    "방어동", "방어진", "일산동", "일산해수욕장", "화정동", "전하동",
    "대송동", "남목", "서부동", "주전", "미포", "동부동",
    "꽃바위", "대왕암", "슬도", "명덕", "화암", "성끝", "봉수로",
    "현대중공업", "HD현대", "미포조선", "조선업", "조선소",
]

NOISE = [
    "바로가기", "새창", "로그인", "회원가입", "사이트맵", "개인정보",
    "이용약관", "저작권", "찾아오시는", "관련사이트", "전체메뉴",
    "홈페이지", "페이스북", "인스타", "유튜브", "블로그", "카카오",
    "소식지", "facebook", "instagram", "youtube", "twitter",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


# ---------------------------------------------------------------- 유틸
def clean(text):
    t = re.sub(r"\s+", " ", (text or "")).strip()
    t = re.sub(r"^(새글|NEW|new|첨부파일)\s*", "", t)
    return t


def short(url):
    p = urlparse(url)
    tail = (p.path or "/")[-45:]
    return f"…{tail}" if p.query else tail


class LegacyAdapter(HTTPAdapter):
    """구형 관공서 서버(낡은 TLS/암호화 방식)에도 접속할 수 있게 하는 어댑터"""

    def init_poolmanager(self, *args, **kwargs):
        if create_urllib3_context is not None:
            ctx = create_urllib3_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
            for level in ("DEFAULT@SECLEVEL=0", "DEFAULT@SECLEVEL=1", "ALL"):
                try:
                    ctx.set_ciphers(level)
                    break
                except Exception:
                    continue
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
            except Exception:
                pass
            kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


SESSION = requests.Session()
SESSION.mount("https://", LegacyAdapter())


def fetch(url, referer=None):
    """관공서 사이트는 느리거나 간헐적으로 막히므로 여러 번 재시도"""
    last = None
    tries = [url]
    if url.startswith("https://"):
        tries.append("http://" + url[8:])

    head = dict(HEADERS)
    if referer:
        head["Referer"] = referer

    for attempt in range(3):
        for u in tries:
            try:
                r = SESSION.get(u, headers=head, timeout=45, verify=False)
                r.raise_for_status()
                if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                    r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            except Exception as e:
                last = e
        time.sleep(3 + attempt * 5)
    raise last


def is_item_link(a, page_host):
    title = clean(a.get_text())
    if len(title) < 8 or len(title) > 200:
        return False
    low = title.lower()
    if any(n.lower() in low for n in NOISE):
        return False
    href = a.get("href", "") or ""
    onclick = a.get("onclick", "") or ""
    # 다른 사이트로 나가는 링크는 게시글이 아님
    if href.startswith("http"):
        if urlparse(href).netloc and urlparse(href).netloc != page_host:
            return False
    hints = ("nttId", "bbsId", "seq", "idx", "no=", "Sn=", "view", "View",
             "detail", "Detail", "select", "board", "Board", "articleNo",
             "javascript", "gosi", "Gosi", "not_ancmt", "nttSn")
    return any(h in href for h in hints) or any(h in onclick for h in hints)


def harvest(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup.find_all(["nav", "header", "footer", "script", "style"]):
        bad.decompose()

    host = urlparse(page_url).netloc
    candidates = []
    for cont in soup.find_all(["table", "tbody", "ul", "ol"]):
        links = [a for a in cont.find_all("a") if is_item_link(a, host)]
        if len(links) >= 3:
            candidates.append((len(links), len(list(cont.parents)), links))

    if not candidates:
        return []

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    links = candidates[0][2]

    items, seen_titles = [], set()
    for a in links:
        title = clean(a.get_text())
        if title in seen_titles:
            continue
        seen_titles.add(title)
        href = a.get("href", "") or ""
        if href and not href.lower().startswith("javascript"):
            link = urljoin(page_url, href)
        else:
            link = page_url
        items.append({"title": title, "link": link})
    return items[:40]


def harvest_deep(start_urls, max_depth=2):
    """주소를 차례로 열어보고, 목록이 안 보이면 iframe 안까지 따라 들어간다"""
    queue = [(u, 0, None) for u in start_urls]
    visited, trace = set(), []
    best_items, best_url = [], None

    while queue:
        url, depth, referer = queue.pop(0)
        if url in visited or len(visited) > 12:
            continue
        visited.add(url)

        try:
            html = fetch(url, referer=referer)
        except Exception as e:
            trace.append(f"{short(url)} → 실패 {type(e).__name__}: {str(e)[:110]}")
            continue

        items = harvest(html, url)
        trace.append(f"{short(url)} → {len(items)}건")
        if len(items) > len(best_items):
            best_items, best_url = items, url
        if len(items) >= 5:
            break

        if depth < max_depth:
            soup = BeautifulSoup(html, "html.parser")
            frames = []
            for fr in soup.find_all(["iframe", "frame"]):
                src = fr.get("src") or ""
                if src and not src.startswith("about:"):
                    frames.append(urljoin(url, src))
            if frames:
                trace.append(f"   ↳ 프레임 {len(frames)}개 발견")
                for f in frames:
                    queue.append((f, depth + 1, url))

    return best_items, best_url, trace


def grade(title, mode):
    if mode == "donggu_only":
        if not any(k in title for k in DONGGU_KEYS):
            return None
        return "🔴" if any(k in title for k in HOT) else "🔵"
    if any(k in title for k in HOT):
        return "🔴"
    if any(k in title for k in WARM):
        return "🟡"
    return "⚪"


def key_of(tag, title):
    return hashlib.sha1(f"{tag}|{title}".encode("utf-8")).hexdigest()[:16]


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": []}


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(text):
    if not TOKEN or not CHAT_ID:
        print("[!] 텔레그램 토큰/채팅ID 없음")
        print(text)
        return
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": chunk,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=20,
            )
        except Exception as e:
            print("[!] 전송 실패:", e)


# ---------------------------------------------------------------- 메인
def main():
    now = datetime.now(KST)
    state = load_state()
    seen = set(state.get("seen", []))

    diag_lines, blocks, new_keys = [], [], []

    for src in SOURCES:
        items, used_url, trace = harvest_deep(src["urls"])

        if DIAG:
            diag_lines.append(f"\n<b>[{esc(src['name'])}]</b>")
            for t in trace:
                diag_lines.append(esc(t))
            diag_lines.append(f"채택: {esc(short(used_url or '없음'))} / {len(items)}건")
            for it in items[:10]:
                diag_lines.append(f"· {esc(it['title'][:70])}")
            continue

        picked = []
        for it in items:
            g = grade(it["title"], src["filter"])
            if g is None:
                continue
            k = key_of(src["tag"], it["title"])
            if k in seen:
                continue
            new_keys.append(k)
            picked.append((g, it))

        if picked:
            picked.sort(key=lambda x: {"🔴": 0, "🟡": 1, "🔵": 1, "⚪": 2}[x[0]])
            lines = [f"<b>[{esc(src['name'])}] 신규 {len(picked)}건</b>"]
            for g, it in picked[:20]:
                lines.append(f'{g} <a href="{it["link"]}">{esc(it["title"][:90])}</a>')
            blocks.append("\n".join(lines))

    if DIAG:
        send("🔎 <b>고시공고 봇 진단 모드 v4</b>\n" + "\n".join(diag_lines))
        return

    first_run = not state.get("seen")
    state["seen"] = list(seen | set(new_keys))
    save_state(state)

    if first_run:
        send(f"📋 <b>고시공고 봇 가동 시작</b> ({now:%m/%d %H:%M})\n"
             f"현재 게시물 {len(new_keys)}건을 기준으로 등록했습니다. "
             f"다음 실행부터 새 공고만 알립니다.")
        return

    if not blocks:
        print("새 공고 없음")
        return

    head = f"📋 <b>고시공고</b> ({now:%m/%d %H:%M})\n🔴 현안 가능 · 🟡 사업·공모 · ⚪ 일반\n"
    send(head + "\n\n" + "\n\n".join(blocks))


if __name__ == "__main__":
    main()
