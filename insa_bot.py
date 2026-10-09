# -*- coding: utf-8 -*-
"""
기노위 피감기관 임원 인사 알림 (동구뉴스봇 추가 모듈)

- 기사: 구글뉴스 RSS에서 '감시 기관명 + 인사 사건어'(공모·선임·취임·직무정지 등)가 제목에 함께 있는 기사만
- 게시판: 기관 임추위·임원모집 게시판에 '임원' 관련 새 글이 뜨면 알림
- 명단: 이사회·임원 명단 페이지에 새 줄(새 이름·직위)이 생기면 알림
- 새 것이 없으면 아무것도 보내지 않음

환경변수
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  (필수, 기존 뉴스봇 값 그대로)
  INSA_CHAT_ID   (선택) 인사 알림만 다른 채널로 보내고 싶을 때
  DIAG=1         (선택) 진단 모드: 출처별 접속 결과를 텔레그램으로 보냄
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
STATE_FILE = "insa_seen.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# ───────────────────────── 감시 기관 (기사 검색용) ─────────────────────────
# "검색어": [제목에서 찾을 이름들]
INSTITUTIONS = {
    # 노동
    "근로복지공단": ["근로복지공단", "근복공"],
    "한국산업인력공단": ["산업인력공단"],
    "한국산업안전보건공단": ["산업안전보건공단", "안전보건공단"],
    "한국장애인고용공단": ["장애인고용공단"],
    "한국고용정보원": ["고용정보원"],
    "한국폴리텍대학": ["폴리텍"],
    "한국기술교육대학교": ["기술교육대", "한기대", "코리아텍"],
    "노사발전재단": ["노사발전재단"],
    "건설근로자공제회": ["건설근로자공제회"],
    "한국사회적기업진흥원": ["사회적기업진흥원"],
    "한국잡월드": ["잡월드"],
    "한국고용노동교육원": ["고용노동교육원"],
    # 환경·기상
    "한국수자원공사": ["수자원공사", "K-water", "수공"],
    "한국환경공단": ["환경공단"],
    "국립공원공단": ["국립공원공단"],
    "수도권매립지관리공사": ["매립지관리공사", "매립지공사", "SL공사"],
    "한국환경산업기술원": ["환경산업기술원"],
    "국립생태원": ["국립생태원"],
    "국립낙동강생물자원관": ["낙동강생물자원관"],
    "국립호남권생물자원관": ["호남권생물자원관"],
    "한국상하수도협회": ["상하수도협회"],
    "한국환경보전원": ["환경보전원"],
    "한국수자원조사기술원": ["수자원조사기술원"],
    "한국기상산업기술원": ["기상산업기술원"],
    "APEC기후센터": ["APEC기후센터", "APEC 기후센터"],
    "한국수치모델개발원": ["수치모델개발원"],
    # 에너지
    "한국전력공사": ["한국전력", "한전"],
    "한국수력원자력": ["한국수력원자력", "한수원"],
    "한국남동발전": ["남동발전"],
    "한국중부발전": ["중부발전"],
    "한국서부발전": ["서부발전"],
    "한국남부발전": ["남부발전"],
    "한국동서발전": ["동서발전"],
    "한전KPS": ["한전KPS", "한전 KPS"],
    "한전KDN": ["한전KDN", "한전 KDN"],
    "한국전력거래소": ["전력거래소"],
    "한전MCS": ["한전MCS", "한전 MCS"],
    "한국원자력환경공단": ["원자력환경공단"],
    "한전원자력연료": ["한전원자력연료"],
    "한국전력국제원자력대학원대학교": ["국제원자력대학원", "KINGS"],
    "한국전력기술": ["한국전력기술", "한전기술"],
    "한국에너지공단": ["에너지공단"],
    "한국지역난방공사": ["지역난방공사", "난방공사"],
    "한국전기안전공사": ["전기안전공사"],
    "한국에너지기술평가원": ["에너지기술평가원"],
    "한국에너지재단": ["에너지재단"],
    "한국에너지정보문화재단": ["에너지정보문화재단"],
}

# 기관명과 상관없이 넓게 보는 검색어 (제목에 감시 기관명이 있어야 통과)
EXTRA_QUERIES = [
    "기후에너지환경부 공공기관 임원 임명",
    "고용노동부 산하기관 이사장 임명",
    "공공기관운영위원회 사장 선임",
]

# 기사 제목에 반드시 있어야 하는 '인사 사건어'
EVENT_RE = re.compile(
    r"공모|재공모|선임|취임|임명|내정|사퇴|사의|해임|직무정지|직무대행|연임|후보|임추위|"
    r"임원추천|신임|차기|후임|공석|낙마|취업심사|\[인사\]|인사\]|이임|퇴임|중도\s*하차"
)
# 기사 제목에서 빼는 잡음
NEWS_NOISE_RE = re.compile(r"신입|인턴|체험형|채용형|공채|직원\s*채용|사장님|공모전|공모사업|공모주|청약")

QUERY_EVENT = "(공모 OR 선임 OR 취임 OR 임명 OR 내정 OR 사퇴 OR 해임 OR 직무정지 OR 후임 OR 신임 OR 임추위)"

# ───────────────────────── 게시판·명단 출처 ─────────────────────────
# kind: board = 새 글 감지 / roster = 명단에 새 줄 감지
SOURCES = [
    # 노동
    ("한국잡월드", "board", "https://www.koreajobworld.or.kr/company/boardCompList.do?bid=35&portalMenuNo=202"),
    ("한국폴리텍대학", "board", "https://www.kopo.ac.kr/board.do?menu=10523"),
    ("한국기술교육대학교", "roster", "https://www.koreatech.ac.kr/menu.es?mid=a10807010000"),
    ("한국장애인고용공단", "roster", "https://www.kead.or.kr/councillgroup/cntntsPage.do?menuId=MENU2162"),
    # 환경·기상
    ("한국환경공단", "board", "https://www.keco.or.kr/web/lay1/bbs/S1T106C995/A/50/list.do"),
    ("한국환경산업기술원", "board", "https://www.keiti.re.kr/site/keiti/ex/board/List.do?cbIdx=277&searchExt1=24000200"),
    ("국립생태원", "board", "https://www.nie.re.kr/nie/bbs/BMSR00086/list.do?gubunCd=EMPMN_001&menuNo=200350"),
    ("한국수자원조사기술원", "board", "https://www.kihs.re.kr/kor_sub/bbs/recruit_list.do"),
    ("한국환경보전원", "board", "https://www.keci.or.kr/web/main.do"),
    ("한국기상산업기술원", "board", "https://www.kmiti.or.kr/kr/board/kmi_notice/boardList.do"),
    ("APEC기후센터", "board", "https://www.apcc21.org/board/BBSMSTR_000000000017?lang=ko"),
    ("한국수치모델개발원", "board", "https://www.kiaps.org/news-pr/careers"),
    # 에너지
    ("한국전력공사", "roster", "https://www.kepco.co.kr/home/esg/governance/directors/composition/conts.do"),
    ("한국서부발전", "board", "https://www.iwest.co.kr/iwest/919/subview.do"),
    ("한국동서발전", "board", "https://www.ewp.co.kr/kor/subpage/content.html?pc=SP5RQGKR3BAUE4W1XB8Q9IE8WF9WA4U"),
    ("한국동서발전 임추위", "board", "https://www.ewp.co.kr/kor/subpage/content.html?pc=GWWBKCH4USQVAEAITU15J47XJAN3SMI"),
    ("한전KPS", "board", "https://www.kps.co.kr/web/esg/governance/board/notice.do"),
    ("한국전력기술", "roster", "https://www.kepco-enc.com/menu.es?mid=a10404010101"),
    ("한국전력거래소", "roster", "https://www.kpx.or.kr/menu.es?mid=a10302020100"),
    ("한국원자력환경공단", "board", "https://www.korad.or.kr/korad/board/index.do?menu_idx=281&manage_idx=62"),
    ("한전MCS", "board", "https://www.kepcomcs.co.kr/mber/open-manage/recommend_list"),
    # 통합 (열리지 않을 수 있음 — 진단 모드에서 확인)
    ("공공기관 채용정보(job.alio)", "board", "https://job.alio.go.kr/recruit.do"),
]

# 게시판 글 제목에 있어야 하는 말
EXEC_RE = re.compile(
    r"임원|사장|이사장|원장|관장|총장|감사|상임이사|비상임이사|운영이사|노동이사|"
    r"임원추천위원회|임추위|후보자\s*모집|초빙|공개모집"
)
# 게시판에서 빼는 직원 채용·잡글
BOARD_NOISE_RE = re.compile(
    r"신입|인턴|체험형|기간제|계약직|무기계약|청년|단시간|대체인력|육아휴직|경력직|직원|"
    r"연구원\s*채용|합격자|서류전형|면접전형|필기|최종\s*합격|감사\s*결과|감사보고|자체감사|"
    r"내부감사|감사실\s*운영|감사인|외부감사|로그인|사이트맵|바로가기|copyright"
)
# 명단 페이지에서 볼 줄
ROSTER_RE = re.compile(r"사장|이사장|원장|총장|감사|이사|본부장|부사장")


# ───────────────────────── 공통 ─────────────────────────
def now_kst():
    return datetime.now(KST)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"news": {}, "board": {}, "roster": {}, "fails": {}, "initialized": False}


def save_state(st):
    # 오래된 기사 기록 정리(60일)
    cutoff = (now_kst() - timedelta(days=60)).strftime("%Y-%m-%d")
    st["news"] = {k: v for k, v in st["news"].items() if v >= cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def h(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def fetch(url, timeout=25):
    """공공기관 사이트는 인증서 체인이 불완전한 경우가 많아, 실패 시 한 번 더 시도."""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
    except requests.exceptions.SSLError:
        requests.packages.urllib3.disable_warnings()
        r = requests.get(url, headers=UA, timeout=timeout, verify=False)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding
    return r.text


def send(text, chat_id, token):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 3800:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf)
    for c in chunks:
        r = requests.post(url, data={"chat_id": chat_id, "text": c,
                                     "parse_mode": "HTML",
                                     "disable_web_page_preview": "true"}, timeout=20)
        if r.status_code != 200:
            print("텔레그램 전송 실패:", r.status_code, r.text[:300])
        time.sleep(1)


# ───────────────────────── 기사 ─────────────────────────
def google_news(query):
    q = urllib.parse.quote(f"{query} when:2d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    xml = fetch(url)
    root = ET.fromstring(xml)
    out = []
    for it in root.iter("item"):
        title = norm(it.findtext("title"))
        link = it.findtext("link") or ""
        src = it.find("source")
        press = norm(src.text) if src is not None and src.text else ""
        if press and title.endswith(" - " + press):
            title = title[: -len(" - " + press)]
        out.append((title, link, press))
    return out


def match_inst(title):
    for name, aliases in INSTITUTIONS.items():
        for a in aliases:
            if a in title:
                # '한전' 은 한전KPS·KDN·MCS·원자력연료·기술과 구분
                if a == "한전" and re.search(r"한전\s*(KPS|KDN|MCS|원자력연료|기술)", title):
                    continue
                return name
    return None


def collect_news(st, diag):
    found, errors = [], []
    seen_titles = set()
    queries = [f'"{n}" {QUERY_EVENT}' for n in INSTITUTIONS] + EXTRA_QUERIES
    for q in queries:
        try:
            items = google_news(q)
        except Exception as e:
            errors.append(f"{q[:20]}…: {e.__class__.__name__}")
            continue
        for title, link, press in items:
            if not EVENT_RE.search(title) or NEWS_NOISE_RE.search(title):
                continue
            inst = match_inst(title)
            if not inst:
                continue
            key = h(re.sub(r"[^가-힣A-Za-z0-9]", "", title))
            if key in st["news"] or key in seen_titles:
                continue
            seen_titles.add(key)
            st["news"][key] = now_kst().strftime("%Y-%m-%d")
            found.append((inst, title, link, press))
        time.sleep(0.7)
    return found, errors


# ───────────────────────── 게시판·명단 ─────────────────────────
def board_items(page_html, base):
    soup = BeautifulSoup(page_html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    items = {}
    for a in soup.find_all("a"):
        text = norm(a.get_text(" ", strip=True))
        if not (6 <= len(text) <= 160):
            continue
        if not EXEC_RE.search(text) or BOARD_NOISE_RE.search(text):
            continue
        href = a.get("href") or ""
        link = base if (not href or href.startswith("#") or href.lower().startswith("javascript")) \
            else urllib.parse.urljoin(base, href)
        items[h(text)] = (text, link)
    # 링크 없이 표로만 된 게시판 대비
    if not items:
        for td in soup.find_all(["td", "li"]):
            text = norm(td.get_text(" ", strip=True))
            if 8 <= len(text) <= 160 and EXEC_RE.search(text) and not BOARD_NOISE_RE.search(text):
                items[h(text)] = (text, base)
    return items


def roster_lines(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    for t in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        t.decompose()
    lines = {}
    for raw in soup.get_text("\n").split("\n"):
        line = norm(raw)
        if 3 <= len(line) <= 120 and ROSTER_RE.search(line) and re.search(r"[가-힣]{2,}", line):
            lines[h(line)] = line
    return lines


def collect_sources(st, diag):
    new_posts, roster_changes, warnings, diag_lines = [], [], [], []
    for name, kind, url in SOURCES:
        sid = h(url)
        try:
            page = fetch(url)
            if kind == "board":
                items = board_items(page, url)
                prev = st["board"].get(sid)
                if prev is None:
                    st["board"][sid] = list(items.keys())
                else:
                    for k, (text, link) in items.items():
                        if k not in prev:
                            new_posts.append((name, text, link))
                    st["board"][sid] = list(set(prev) | set(items.keys()))[-500:]
                sample = " / ".join(t for t, _ in list(items.values())[:2])
                diag_lines.append(f"✅ {name}: 임원 관련 {len(items)}건" + (f" — {sample[:80]}" if sample else ""))
            else:
                lines = roster_lines(page)
                prev = st["roster"].get(sid)
                if prev is None or not prev:
                    st["roster"][sid] = list(lines.keys())
                else:
                    added = [lines[k] for k in lines if k not in prev]
                    # 한 번에 너무 많이 바뀌면 페이지 개편으로 보고 한 줄만
                    if 0 < len(added) <= 8:
                        for line in added:
                            roster_changes.append((name, line, url))
                    elif len(added) > 8:
                        roster_changes.append((name, f"명단 페이지 대폭 변경({len(added)}줄) — 직접 확인", url))
                    if lines:
                        st["roster"][sid] = list(lines.keys())
                diag_lines.append(f"✅ {name}(명단): {len(lines)}줄")
            if st["fails"].get(sid, 0) >= 3:
                warnings.append(("복구", name))
            st["fails"][sid] = 0
        except Exception as e:
            n = st["fails"].get(sid, 0) + 1
            st["fails"][sid] = n
            if n == 3:
                warnings.append(("접속 실패 3회 연속(이후 조용히 재시도)", name))
            diag_lines.append(f"❌ {name}: {e.__class__.__name__} {str(e)[:60]}")
        time.sleep(1)
    return new_posts, roster_changes, warnings, diag_lines


# ───────────────────────── 메인 ─────────────────────────
def a(text, link):
    return f'<a href="{html.escape(link, quote=True)}">{html.escape(text)}</a>'


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = (os.environ.get("INSA_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    diag = os.environ.get("DIAG", "").strip() in ("1", "true", "True", "yes")
    if not token or not chat:
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 가 비어 있음 — Secrets 이름 확인 필요")
        sys.exit(1)

    st = load_state()
    first = not st.get("initialized")

    news, news_err = collect_news(st, diag)
    posts, rosters, warns, diag_lines = collect_sources(st, diag)
    stamp = now_kst().strftime("%m/%d %H:%M")

    if first:
        st["initialized"] = True
        ok = sum(1 for d in diag_lines if d.startswith("✅"))
        msg = [f"<b>[임원 인사 알림] 감시 시작 {stamp}</b>",
               f"게시판·명단 {len(SOURCES)}곳 중 {ok}곳 접속 정상, 현재 글·명단은 기준으로 저장(알림 없음)",
               f"최근 2일 인사 기사 {len(news)}건:"]
        for inst, title, link, press in news[:15]:
            msg.append(f"· [{html.escape(inst)}] {a(title, link)} — {html.escape(press)}")
        msg.append("")
        msg += [html.escape(d) for d in diag_lines]
        send("\n".join(msg), chat, token)
        save_state(st)
        return

    if diag:
        msg = [f"<b>[임원 인사 알림] 진단 {stamp}</b>"] + [html.escape(d) for d in diag_lines]
        msg.append(f"기사 검색 오류 {len(news_err)}건" + (": " + html.escape("; ".join(news_err[:5])) if news_err else ""))
        msg.append(f"이번 회차 새 기사 {len(news)}건, 새 게시글 {len(posts)}건, 명단 변경 {len(rosters)}건")
        send("\n".join(msg), chat, token)

    if news or posts or rosters or warns:
        msg = [f"<b>[임원 인사 알림] {stamp}</b>"]
        if posts:
            msg.append(f"\n■ 새 공고·게시글 {len(posts)}건")
            for name, text, link in posts:
                msg.append(f"· {html.escape(name)} — {a(text, link)}")
        if rosters:
            msg.append(f"\n■ 임원 명단 변경 {len(rosters)}건")
            for name, line, link in rosters:
                msg.append(f"· {html.escape(name)} — {a(line, link)}")
        if news:
            msg.append(f"\n■ 기사 {len(news)}건")
            for inst, title, link, press in news:
                msg.append(f"· [{html.escape(inst)}] {a(title, link)} — {html.escape(press)}")
        if warns:
            groups = {}
            for kind, nm in warns:
                groups.setdefault(kind, []).append(nm)
            for kind, names in groups.items():
                msg.append("\n※ " + html.escape(f"{kind}: {', '.join(names)}"))
        send("\n".join(msg), chat, token)
    else:
        print("새 항목 없음 — 전송 안 함")

    save_state(st)


if __name__ == "__main__":
    main()
