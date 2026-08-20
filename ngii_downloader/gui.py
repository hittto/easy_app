#!/usr/bin/env python3
"""국토정보맵 자동 다운로더 - GUI"""

import ctypes
import asyncio
import os
import queue
import json
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import tkinter as tk
import winreg
from tkinter import ttk, scrolledtext, messagebox

# ── DPI 인식 + 스케일 계산 (텍스트 선명도 + 창 크기 정상화) ─────────────────
_DPI_SCALE = 1.0
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Per-Monitor DPI Aware
    _DPI_SCALE = ctypes.windll.shcore.GetDpiForSystem() / 96
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── 상수 ────────────────────────────────────────────────────────────────────
CDP_URL   = "http://localhost:9222"
CDP_PORT  = 9222
MAP_URL   = "https://map.ngii.go.kr/ms/map/NlipMap.do"
MAP_URL_KEYWORD = "NlipMap"

# ── 디자인 토큰 — Apple HIG 레이아웃/여백/타이포 규칙 + 국토정보플랫폼(map.ngii.go.kr)
# 실제 색상 팔레트를 결합. 색상 값은 사이트를 직접 렌더링해 헤더·활성 탭·버튼·본문의
# computed style에서 추출한 실측 hex(주조색 #0D624F, 포인트색 #E4113B, 본문 그레이
# #333333/#666666/#999999/#F5F5F5/#EEEEEE 등)를 그대로 사용한다.
C_BG        = "#F5F5F5"  # 사이트 배경 그레이 — 윈도우 배경
C_CARD      = "#FFFFFF"  # 사이트 콘텐츠 카드 배경(흰색) — 섹션(그룹) 배경
C_BORDER    = "#E9E9E9"  # 사이트 구분선 그레이
C_DIVIDER   = "#F0F0F0"  # 더 옅은 내부 구분선
C_FG        = "#333333"  # 사이트 본문 텍스트 그레이 — 기본 텍스트
C_FG2       = "#666666"  # 사이트 보조 텍스트 그레이
C_FG3       = "#999999"  # 사이트 placeholder/비활성 그레이
C_ENTRY     = "#FFFFFF"  # 입력 필드
C_SEL       = "#E2ECEA"  # 주조색 12% 틴트 — 선택/포커스 하이라이트
C_ACC       = "#0D624F"  # 국토정보플랫폼 주조색(헤더·활성 탭) — controlAccentColor
C_ACC_H     = "#0A4F3F"  # accent pressed (주조색 20% 어둡게)
C_SUCCESS   = "#0D624F"  # 주조색과 통일 (연결됨/성공 상태)
C_SUCCESS_H = "#0A4F3F"
C_DANGER    = "#E4113B"  # 국토정보플랫폼 포인트 레드(강조/경고 색상)
C_DANGER_H  = "#B60E2F"
C_OK        = "#0D624F"
C_ERR       = "#E4113B"

# 사용자 배율 (런타임에 변경 가능, 기본 100%)
_USER_ZOOM = 1.0

# 기본 창 크기 — 카테고리 세그먼트 컨트롤(5개)과 "추가 ▶" 버튼이 잘리지 않도록
# 가로를 넉넉하게 잡고, "작업 데이터 목록"(복수 데이터 설정)이 추가되면서 세로도
# 그만큼 키웠다. 이 값은 동시에 창의 최소 크기이기도 하다 — _apply_window_size()가
# geometry와 minsize에 같은 값을 적용해, 컨트롤이 잘릴 만큼 작게는 줄일 수 없다.
WINDOW_W = 1280
WINDOW_H = 1240

# 실행 로그 창의 기본 줄 수와, 화면이 작아 창을 다 못 펼칠 때 줄여도 되는 최소 줄 수.
# _apply_window_size()가 "요구 높이 > 화면 높이"인 경우에만 LOG_LINES → LOG_MIN_LINES
# 방향으로 줄여서 전체 레이아웃이 잘리지 않게 한다.
LOG_LINES = 12
LOG_MIN_LINES = 5
# 목록(리스트박스)들의 기본 줄 수
SGG_LINES, REGION_LINES, DATA_LINES = 6, 5, 6
# 화면이 작아 전체 레이아웃이 안 들어갈 때 줄여 나갈 순서 — (위젯 속성명, 줄일 하한).
# 스크롤이 있어 잃는 게 가장 적은 로그부터 줄이고, 그래도 모자라면 목록들을,
# 마지막으로 로그를 한 번 더 줄인다.
SHRINK_PLAN = (
    ("log_text", 8),
    ("sgg_lb", 3),
    ("data_lb", 3),
    ("region_lb", 3),
    ("log_text", LOG_MIN_LINES),
    # 여기까지 줄여도 안 들어가는 아주 작은 화면(1366×768 등)을 위한 마지막 단계
    ("sgg_lb", 2),
    ("data_lb", 2),
    ("region_lb", 2),
    ("log_text", 3),
)

# 다운로드 실패 시 같은 항목을 몇 번까지 다시 시도할지 (초과하면 다음 항목으로 진행)
MAX_ATTEMPTS = 3
# 재시도 사이 대기 시간(초) — 시도 횟수에 비례해 늘어난다(1회차 2초, 2회차 4초)
RETRY_DELAY_SEC = 2.0

# DPI × 사용자 배율 적용 폰트 크기
def _fs(size: int) -> int:
    return max(1, round(size * _DPI_SCALE * _USER_ZOOM))

# 타이포그래피 — HIG 텍스트 스타일(Large Title/Headline/Body/Footnote)의 크기·굵기
# 위계를 따르되, 한글 렌더링을 위해 서체는 Windows의 Apple SD Gothic Neo 대응 서체인
# "맑은 고딕"을 사용한다 (SF Pro/Segoe UI는 한글 글리프를 지원하지 않음).
_FONT_FAMILY = "Malgun Gothic"
FONT_LARGETITLE = (_FONT_FAMILY, _fs(15), "bold")   # 앱 타이틀
FONT_TITLE = (_FONT_FAMILY, _fs(11), "bold")        # 섹션 헤드라인
FONT_B     = (_FONT_FAMILY, _fs(9),  "bold")        # 강조 본문 / 버튼 라벨
FONT       = (_FONT_FAMILY, _fs(9))                 # 본문
FONT_SMALL = (_FONT_FAMILY, _fs(8))                 # 캡션/풋노트
FONT_LOG   = ("Consolas",   _fs(9))                 # 로그 (SF Mono 대응 고정폭 서체)


def parse_cdp_url(raw: str) -> tuple[str, int, str]:
    """입력창에 적힌 값에서 (호스트, 포트, 정규화된 URL)을 뽑는다.

    "9333", "localhost:9333", "http://127.0.0.1:9333" 처럼 사람이 실제로 적는
    형태를 모두 받아준다. 파싱에 실패하면 기본값(localhost:9222)으로 되돌린다."""
    raw = (raw or "").strip()
    if not raw:
        return "localhost", CDP_PORT, f"http://localhost:{CDP_PORT}"
    if raw.isdigit():                      # 포트 번호만 적은 경우
        raw = f"http://localhost:{raw}"
    elif "://" not in raw:                 # "localhost:9333" 처럼 적은 경우
        raw = "http://" + raw
    try:
        u = urllib.parse.urlparse(raw)
        host = u.hostname or "localhost"
        port = u.port or CDP_PORT
    except ValueError:
        host, port = "localhost", CDP_PORT
    return host, int(port), f"http://{host}:{port}"


def cdp_candidates(host: str, port: int) -> list:
    """연결을 시도할 CDP URL 목록.

    Chrome은 --remote-debugging-port를 127.0.0.1에만 바인딩하는데, 환경에 따라
    "localhost"가 IPv6(::1)로 먼저 풀려 연결이 실패하는 경우가 있다. 로컬 호스트일
    때는 127.0.0.1을 먼저 시도하고 localhost를 예비로 둔다."""
    if host in ("localhost", "127.0.0.1", "::1", ""):
        return [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    return [f"http://{host}:{port}"]


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """해당 포트에 누군가 listen 중인지 (CDP인지까지는 보지 않는다)."""
    for h in (["127.0.0.1", "localhost"] if host in ("localhost", "127.0.0.1", "") else [host]):
        try:
            with socket.create_connection((h, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def probe_cdp_version(host: str, port: int, timeout: float = 2.0) -> str | None:
    """해당 포트가 정말 Chrome DevTools 엔드포인트인지 /json/version 으로 확인한다.
    맞으면 브라우저 버전 문자열, 아니면 None — "포트가 열려는 있는데 연결이 안 된다"는
    상황에서 원인이 '다른 프로그램 점유'인지 'CDP는 맞는데 연결 실패'인지 가른다."""
    hosts = ["127.0.0.1", "localhost"] if host in ("localhost", "127.0.0.1", "") else [host]
    for h in hosts:
        try:
            with urllib.request.urlopen(
                    f"http://{h}:{port}/json/version", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data.get("Browser") or "unknown"
        except Exception:
            continue
    return None


def find_free_port(start: int = 9223, tries: int = 60) -> int | None:
    """start부터 위로 올라가며 비어 있는 TCP 포트를 하나 찾는다 (안내 메시지용)."""
    for p in range(start, start + tries):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
    return None


def find_chrome() -> str | None:
    """어느 PC·계정에서도 Chrome 실행 파일 경로를 반환한다."""
    # 1) 레지스트리 (시스템 / 사용자 설치 공통)
    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
    ]
    for hive, sub in reg_paths:
        try:
            with winreg.OpenKey(hive, sub) as k:
                path = winreg.QueryValue(k, "")
                if path and os.path.isfile(path):
                    return path
        except OSError:
            pass

    # 2) 일반적인 설치 경로
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""),
                     r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None

CATEGORIES = ["인구", "건물", "토지", "국토지표", "국토모니터링"]

# 세그먼트 컨트롤에 표시할 축약 라벨 — 실제 값(CATEGORIES/PANEL_IDS/사이트 버튼 텍스트
# 매칭에 쓰는 문자열)은 그대로 두고, 화면에 그리는 텍스트만 짧게 바꾼다. 목록에 없는
# 카테고리는 원래 이름 그대로 표시된다.
CATEGORY_LABELS = {
    "국토모니터링": "모니터링",
}

PANEL_IDS = {
    "인구":       "statisticsPopulation",
    "건물":       "statisticsBuilding",
    "토지":       "statisticsLand",
    "국토지표":   "statisticsIndicator",
    "국토모니터링": "statisticsMonitoring",
}

SIDO_LIST = [
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
    "대전광역시", "울산광역시", "세종특별자치시", "경기도", "충청북도",
    "충청남도", "전북특별자치도", "전라남도", "경상북도", "경상남도",
    "제주특별자치도", "강원특별자치도",
]

# shpdownload 파라미터 매핑
ITEM_PARAM = {
    "500M": 0.5, "500m": 0.5,
    "1KM": 1.0,  "1km": 1.0,
    "2KM": 2.0,  "2km": 2.0,
    "5KM": 5.0,  "5km": 5.0,
}

# ── 데이터 체인 자동 탐지 ─────────────────────────────────────────────────────
# 카테고리별 select 체인(분류/자료유형/항목 등)을 하드코딩하지 않고, 실행 시점에
# 사이트 DOM을 스캔해 동적으로 구성한다. 아래는 "체인 후보에서 제외할" 이름들 —
# 시도/시군구/기간은 별도 UI(지역 선택, 기간)에서 다루고, "_temp"/"dTypeList"류는
# 사이트 내부용 보조 select라 사용자에게 노출하지 않는다.
CHAIN_EXCLUDE_NAMES = {"sido_text_area", "sgg_text_area", "year_text_area"}
CHAIN_EXCLUDE_SUFFIXES = ("_temp",)
CHAIN_EXCLUDE_CONTAINS = ("dTypeList",)


# 국토모니터링 패널은 실제 사이트에서 확인한 결과, 시도/시군구(sido_text_area/
# sgg_text_area)가 다른 카테고리처럼 처음부터 채워져 있는 게 아니라 year_text_area
# (기준년도)를 먼저 선택해야만 채워진다 — 즉 이 카테고리에서는 기준년도가 "다운로드할
# 기간 범위"가 아니라 분석자료 다음에 고르는 일반 체인 필드다. 그래서 이 카테고리에
# 한해서만 year_text_area를 체인 제외 목록에서 빼준다.
CHAIN_INCLUDE_YEAR_CATEGORIES = {"국토모니터링"}


def is_chain_candidate(name: str, category: str = None) -> bool:
    """select name이 데이터 체인(분류/자료유형/항목 등) 후보인지 판별."""
    if not name:
        return False
    if name in CHAIN_EXCLUDE_NAMES:
        if not (name == "year_text_area" and category in CHAIN_INCLUDE_YEAR_CATEGORIES):
            return False
    if any(name.endswith(suf) for suf in CHAIN_EXCLUDE_SUFFIXES):
        return False
    if any(sub in name for sub in CHAIN_EXCLUDE_CONTAINS):
        return False
    return True


# ── 유틸 ────────────────────────────────────────────────────────────────────

def filter_periods(available: list, sy: str, sm: str, ey: str, em: str) -> list:
    """웹 year_text_area 옵션 중 [sy/sm ~ ey/em] 범위에 속하는 것만 반환.
    '2020년 01월' 형식(월 단위)과 '2020년' 형식(연 단위) 모두 처리."""
    start = (int(sy), int(sm))
    end   = (int(ey), int(em))
    result = []
    for opt in available:
        m = re.match(r'(\d{4})년\s+(\d{1,2})월', opt)
        if m:
            if start <= (int(m.group(1)), int(m.group(2))) <= end:
                result.append(opt)
            continue
        m = re.match(r'(\d{4})년$', opt)
        if m:
            if int(sy) <= int(m.group(1)) <= int(ey):
                result.append(opt)
    return result


# ── 브라우저 자동화 함수 ──────────────────────────────────────────────────────

def _js_find_select(parent_sel, name):
    """parent_sel과 매칭되는 모든 요소를 순회해 name에 해당하는 select를 반환하는 JS 헬퍼 코드"""
    return f"""
        (function() {{
            let s = null;
            for (const p of document.querySelectorAll('{parent_sel}')) {{
                s = p.querySelector('select[name="{name}"]');
                if (s) break;
            }}
            return s;
        }})()
    """


async def jq_set(page, parent_sel, name, value):
    """jQuery change 이벤트와 함께 select 값 설정 (모든 매칭 li 탐색)"""
    await page.evaluate(f"""() => {{
        const s = {_js_find_select(parent_sel, name)};
        if (!s) return;
        s.value = '{value}';
        if (window.jQuery) jQuery(s).trigger('change');
        else s.dispatchEvent(new Event('change', {{bubbles:true}}));
    }}""")


async def wait_for_opts(page, parent_sel, name, min_count=1, timeout_sec=15):
    """select에 min_count개 이상 옵션이 생길 때까지 대기 (wait_for_function 사용 — 빠름)."""
    js = f"""() => {{
        const s = {_js_find_select(parent_sel, name)};
        return s ? Array.from(s.options).filter(o => o.value.trim()).length >= {min_count} : false;
    }}"""
    try:
        await page.wait_for_function(js, timeout=timeout_sec * 1000)
        return True
    except Exception:
        return False


async def wait_read_and_advance(page, parent_sel, name, min_count=1, timeout_sec=15, advance=True):
    """select에 옵션이 채워질 때까지 '페이지 안에서' 직접 폴링(requestAnimationFrame)한 뒤,
    옵션 (value, text) 목록을 읽고 필요하면 첫 번째 옵션으로 값을 설정 + change 이벤트까지
    발생시킨다 — 이 세 동작(대기/읽기/설정)을 CDP 왕복 1회로 처리한다.

    기존에는 wait_for_opts() + get_select_options_text() + jq_set()이 각각 별도의
    CDP 왕복이었는데(체인 필드 1개당 3회), 이를 1회로 합쳐 "옵션 불러오기" 체감 속도를
    끌어올린다. 타임아웃 시 None을 반환한다.
    """
    js = f"""async () => {{
        function findSel() {{
            let s = null;
            for (const p of document.querySelectorAll('{parent_sel}')) {{
                s = p.querySelector('select[name="{name}"]');
                if (s) break;
            }}
            return s;
        }}
        const deadline = Date.now() + {int(timeout_sec * 1000)};
        while (true) {{
            const s = findSel();
            if (s) {{
                const real = Array.from(s.options).filter(o => o.value.trim());
                if (real.length >= {min_count}) {{
                    const opts = real.map(o => ({{v: o.value, t: o.text.trim()}}));
                    if ({"true" if advance else "false"} && opts.length) {{
                        s.value = opts[0].v;
                        if (window.jQuery) jQuery(s).trigger('change');
                        else s.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                    return opts;
                }}
            }}
            if (Date.now() > deadline) return null;
            await new Promise(r => requestAnimationFrame(r));
        }}
    }}"""
    return await page.evaluate(js)


async def get_select_options(page, parent_sel, name):
    """select 요소의 value 목록 반환 (모든 매칭 li 탐색)"""
    return await page.evaluate(f"""() => {{
        const s = {_js_find_select(parent_sel, name)};
        if (!s) return [];
        return Array.from(s.options).map(o => o.value).filter(v => v.trim());
    }}""")


async def get_select_options_text(page, parent_sel, name):
    """select 요소의 (value, text) 목록 반환 (모든 매칭 li 탐색)"""
    return await page.evaluate(f"""() => {{
        const s = {_js_find_select(parent_sel, name)};
        if (!s) return [];
        return Array.from(s.options)
            .filter(o => o.value.trim())
            .map(o => ({{v: o.value, t: o.text.trim()}}));
    }}""")


async def get_panel_select_names(page, parent_sel):
    """parent_sel(패널 li 클래스) 안에 현재 존재하는 모든 select의 (name, 플레이스홀더텍스트)를
    DOM 순서대로 반환. 아직 값이 없는(옵션 1개=플레이스홀더뿐) select도 포함 —
    존재 자체가 '이 단계가 앞으로 쓰일 것'이라는 신호이기 때문."""
    return await page.evaluate(f"""() => {{
        const seen = new Set();
        const out = [];
        for (const p of document.querySelectorAll('{parent_sel}')) {{
            for (const s of p.querySelectorAll('select[name]')) {{
                if (seen.has(s.name)) continue;
                seen.add(s.name);
                const first = s.options.length ? s.options[0].text.trim() : '';
                out.push([s.name, first]);   // (name, placeholder) 튜플 — dict로 반환하면
                                              // 파이썬에서 "for name, label in scan" 언패킹 시
                                              // dict의 key만 순회되어 값이 아니라 "name"/"label"
                                              // 문자열 자체가 들어가는 버그가 생긴다.
            }}
        }}
        return out;
    }}""")


async def find_panel_sel(page, category):
    """카테고리 패널 안에서 보이는 li 셀렉터를 탐색"""
    panel_id = PANEL_IDS.get(category)
    if not panel_id:
        return None
    cls = await page.evaluate(f"""() => {{
        const div = document.getElementById('{panel_id}');
        if (!div) return null;
        for (const li of div.querySelectorAll('li')) {{
            const st = window.getComputedStyle(li);
            if (st.display !== 'none' && li.className.trim()) {{
                const first = li.className.trim().split(' ')[0];
                if (first) return first;
            }}
        }}
        return null;
    }}""")
    if cls:
        return f"#{panel_id} li[class~=\"{cls}\"]"
    return f"#{panel_id} li"


async def activate_category_tab(page, category):
    """상단 카테고리 탭 클릭.

    이미 활성화된(class="on") 탭을 다시 클릭하면 사이트가 changeChildCategory()를
    재실행해 패널 내부를 통째로 리렌더링하는데, 이 과정에서 select 옵션이
    잠깐 비워졌다가 비동기로 다시 채워지는 사이트가 있어 우리 측 폴링이
    그 리셋 타이밍과 겹치면 "옵션 로드 시간 초과"가 발생할 수 있다.
    이미 켜져 있는 탭이면 클릭을 건너뛰어 불필요한 리셋/경쟁 상태를 피한다.
    """
    result = await page.evaluate(f"""() => {{
        const root = document.querySelector('#statWrap, #statisticsWrap, .statistics_wrap, .statWrap')
                  || document.body;
        for (const el of root.querySelectorAll('button, li')) {{
            const txt = (el.innerText || el.textContent || '').trim();
            if (txt === '{category}' && el.offsetParent !== null) {{
                if (el.classList.contains('on')) return 'already_on';
                el.click();
                return 'ok:' + el.tagName;
            }}
        }}
        return 'not_found';
    }}""")
    return result


async def wait_for_element_js(page, js_expr: str, timeout_sec: int = 15) -> bool:
    """JS 표현식이 truthy를 반환할 때까지 대기 (wait_for_function 사용 — 빠름)."""
    try:
        await page.wait_for_function(f"() => !!({js_expr})", timeout=timeout_sec * 1000)
        return True
    except Exception:
        return False



async def get_year_options(page, panel_sel) -> list:
    """year_text_area의 실제 옵션 값 목록 반환."""
    return await page.evaluate(f"""() => {{
        let sel = null;
        for (const p of document.querySelectorAll('{panel_sel}')) {{
            sel = p.querySelector('select[name="year_text_area"]');
            if (sel) break;
        }}
        if (!sel) return [];
        return Array.from(sel.options).map(o => o.value).filter(v => v.trim());
    }}""")


async def close_alert(page):
    """알림/모달 팝업 닫기 — #modalPopup 강제 초기화 포함."""
    await page.evaluate("""() => {
        // 일반 알림 버튼 (확인/닫기)
        for (const b of document.querySelectorAll('button')) {
            const t = (b.innerText || '').trim();
            if ((t === '확인' || t === '닫기') && b.offsetParent !== null) b.click();
        }
        // #modalPopup 강제 닫기 (다운로드 모달이 열려 있을 경우 대비)
        const modal = document.querySelector('#modalPopup');
        if (modal && modal.offsetParent !== null) {
            for (const b of modal.querySelectorAll('button')) {
                const t = (b.innerText || b.textContent || '').trim();
                if (['닫기', '취소', '×', 'X', '확인'].includes(t)) {
                    b.click();
                    return;
                }
            }
            modal.style.display = 'none';
        }
    }""")
    await asyncio.sleep(0.3)


async def wait_for_year_applied(page, target, timeout_sec=10):
    """원천자료 기준년월이 target과 일치할 때까지 대기 (wait_for_function 사용)."""
    js = f"""() => {{
        for (const el of document.querySelectorAll('*')) {{
            if (el.children.length > 0) continue;
            const t = el.innerText || el.textContent || '';
            if (t.includes('{target}')) return true;
        }}
        return false;
    }}"""
    try:
        await page.wait_for_function(js, timeout=timeout_sec * 1000)
        return True
    except Exception:
        return False


# ── GUI 클래스 ───────────────────────────────────────────────────────────────

class NgiiDownloaderGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("국토정보맵 자동 다운로더")
        self.root.resizable(True, True)

        self.log_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()

        self.playwright_obj = None
        self.browser = None
        self.map_page = None
        self._panel_sel: str | None = None
        self.is_running = False
        self._loading_options = False   # 옵션 로딩 중 trace 콜백 차단용

        # 작업 데이터 목록 — "데이터 설정"에서 [추가 ▼]로 쌓아 둔 설정들.
        # 각 원소: {category, chain, chain_vals, chain_vmaps, periods, label}
        self._data_items: list[dict] = []
        # 현재 화면의 체인이 "어느 분류에서" 불러온 것인지 — 분류만 바꾸고
        # [옵션 불러오기] 없이 [추가 ▼]를 누르면 엉뚱한 조합이 담기므로 그걸 막는다.
        self._chain_category: str | None = None

        # ── 배율 설정 ────────────────────────────────────────────────────────
        self._zoom_pct = 100
        self._all_btns: list = []       # tk.Button 참조 (폰트 일괄 갱신용)
        self.zoom_var = tk.StringVar(value="100%")
        self.zoom_var.trace_add("write", self._on_zoom_changed)

        # ── 단일 영속 이벤트 루프 (Playwright 객체 공유를 위해 필수) ──────────
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_loop_thread()

        self._setup_theme()
        self._build_ui()
        self._apply_window_size()
        self._poll_log()

    # ── 창 크기 ──────────────────────────────────────────────────────────────

    def _fit_widgets_to_screen(self, avail_h: int) -> int:
        """전체 레이아웃이 요구하는 높이가 화면(avail_h)을 넘으면, SHRINK_PLAN 순서대로
        로그/목록의 줄 수를 한 줄씩 줄여가며 맞춘다. 조정 후 요구 높이를 반환한다.

        항상 기본 줄 수로 되돌린 뒤 다시 계산하므로, 배율을 낮추거나 큰 화면으로
        옮기면 원래 크기로 복구된다."""
        heights = {"log_text": LOG_LINES, "sgg_lb": SGG_LINES,
                   "data_lb": DATA_LINES, "region_lb": REGION_LINES}
        for attr, n in heights.items():
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.configure(height=n)
                except tk.TclError:
                    pass
        self.root.update_idletasks()
        req = self.root.winfo_reqheight()

        plan = list(SHRINK_PLAN)
        while req > avail_h and plan:
            attr, floor = plan[0]
            w = getattr(self, attr, None)
            if w is None or heights.get(attr, 0) <= floor:
                plan.pop(0)
                continue
            heights[attr] -= 1
            try:
                w.configure(height=heights[attr])
            except tk.TclError:
                plan.pop(0)
                continue
            self.root.update_idletasks()
            req = self.root.winfo_reqheight()
        return req

    def _apply_window_size(self):
        """기본 크기 = 최소 크기.

        고정 상수(WINDOW_W/H × DPI × 배율)만 쓰면, 배율이 높거나 데이터 체인이 길어
        위젯이 실제로 요구하는 높이가 화면을 넘을 때 아래쪽(실행 로그)이 잘린다.
        그래서 tk이 계산한 **실제 요구 크기**(winfo_reqwidth/reqheight)를 함께 보고,
        - 요구 크기보다는 반드시 크게(→ 기본 크기에서 로그까지 다 보이게),
        - 화면보다는 작게(→ 창을 옮기거나 줄일 수 있게)
        창 크기를 잡는다. 화면이 너무 작아 둘을 동시에 만족할 수 없으면
        _fit_widgets_to_screen()이 로그·목록 줄 수를 줄여 요구 높이를 낮춘다."""
        base_w = int(WINDOW_W * _DPI_SCALE * _USER_ZOOM)
        base_h = int(WINDOW_H * _DPI_SCALE * _USER_ZOOM)
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except tk.TclError:
            sw, sh = base_w + 20, base_h + 80
        avail_w = max(640, sw - 20)
        avail_h = max(480, sh - 80)     # 작업 표시줄 여유

        req_h = self._fit_widgets_to_screen(avail_h)
        req_w = self.root.winfo_reqwidth()

        w = min(max(base_w, req_w), avail_w)
        h = min(max(base_h, req_h), avail_h)
        self.root.minsize(w, h)
        self.root.geometry(f"{w}x{h}")

    def _setup_theme(self):
        """Apple Human Interface Guidelines 라이트 모드에 맞춘 테마.

        macOS의 windowBackgroundColor/systemGroupedBackground/labelColor 등
        시맨틱 컬러 위계를 따르고, 섹션 제목은 (HIG가 권장하듯) 강조색이 아닌
        Headline 텍스트 스타일의 중립 색으로 표기해 "색상은 인터랙션/상태에만
        아껴 쓴다"는 원칙을 지켰다."""
        self.root.configure(bg=C_BG)
        s = ttk.Style(self.root)
        s.theme_use("clam")

        s.configure("TFrame",      background=C_BG)
        s.configure("Card.TFrame", background=C_CARD)

        # 그룹(섹션) 컨테이너 — 옅은 hairline 구분선 하나로만 경계를 표시
        # labelmargins = (좌, 상, 우, 하) — 카드 제목이 테두리 왼쪽 위 모서리에
        # 딱 붙어 보이지 않도록 안쪽으로 들여쓰고 아래 간격도 준다.
        s.configure("TLabelframe",
                    background=C_CARD, relief="flat",
                    borderwidth=1, bordercolor=C_BORDER,
                    labelmargins=(_fs(10), _fs(2), 0, _fs(6)))
        s.configure("TLabelframe.Label",
                    background=C_CARD, foreground=C_FG,   # Headline: 중립색 (accent 남용 금지)
                    font=FONT_TITLE)

        s.configure("TLabel",
                    background=C_BG, foreground=C_FG, font=FONT)
        s.configure("Card.TLabel",
                    background=C_CARD, foreground=C_FG, font=FONT)
        s.configure("Muted.TLabel",
                    background=C_CARD, foreground=C_FG2, font=FONT)
        s.configure("Small.TLabel",
                    background=C_CARD, foreground=C_FG3, font=FONT_SMALL)
        s.configure("Bg.TLabel",
                    background=C_BG, foreground=C_FG2, font=FONT)

        s.configure("TEntry",
                    fieldbackground=C_ENTRY, foreground=C_FG,
                    insertcolor=C_ACC, font=FONT,
                    bordercolor=C_BORDER,
                    lightcolor=C_BORDER, darkcolor=C_BORDER)
        s.map("TEntry",
              bordercolor=[("focus", C_ACC)])

        s.configure("TCombobox",
                    fieldbackground=C_ENTRY, foreground=C_FG,
                    selectbackground=C_SEL, selectforeground=C_FG,
                    font=FONT, bordercolor=C_BORDER, arrowcolor=C_FG2,
                    lightcolor=C_ENTRY, darkcolor=C_ENTRY, arrowsize=_fs(12))
        s.map("TCombobox",
              fieldbackground=[("readonly", C_ENTRY), ("disabled", C_BG)],
              foreground=[("disabled", C_FG3)],
              bordercolor=[("focus", C_ACC)])

        # 프로그레스 바 — controlAccentColor(systemBlue)
        s.configure("Horizontal.TProgressbar",
                    troughcolor=C_DIVIDER, background=C_ACC,
                    lightcolor=C_ACC, darkcolor=C_ACC,
                    bordercolor=C_DIVIDER, thickness=_fs(6))

        s.configure("TScrollbar",
                    background=C_DIVIDER, troughcolor=C_CARD,
                    bordercolor=C_CARD, arrowcolor=C_FG3,
                    relief="flat", width=10)
        s.map("TScrollbar",
              background=[("active", C_FG3)])

        # 세그먼트 컨트롤(카테고리 선택)용 스타일 — 선택/비선택 두 상태
        s.configure("SegBg.TFrame", background=C_BORDER)
        s.configure("SegOn.TButton",
                    background=C_ACC, foreground="#FFFFFF",
                    font=FONT_B, borderwidth=0, focuscolor=C_ACC,
                    padding=(8, 5))
        s.map("SegOn.TButton",
              background=[("active", C_ACC_H)])
        s.configure("SegOff.TButton",
                    background=C_CARD, foreground=C_FG2,
                    font=FONT, borderwidth=0, focuscolor=C_CARD,
                    padding=(8, 5))
        s.map("SegOff.TButton",
              background=[("active", C_DIVIDER)],
              foreground=[("active", C_FG)])

    def _mk_btn(self, parent, text, command, style="primary",
                state="normal", width=None, **kw):
        """HIG 버튼 위계를 따르는 스타일드 버튼.

        primary/success/danger = "Prominent"(강조 채움) 버튼 — 뷰당 핵심 동작 하나에만 사용.
        secondary = "Bordered"(테두리 버튼) — 옅은 hairline 테두리의 중립 버튼.
        ghost     = "Borderless"(텍스트형) 버튼 — 배경 없이 텍스트만.
        Tk 위젯 특성상 완전한 둥근 모서리(Liquid Glass pill)는 구현할 수 없어
        플랫 컬러 + 넉넉한 여백으로 근사했다."""
        palettes = {
            "primary":   (C_ACC,     C_ACC_H,      "#FFFFFF", False),
            "success":   (C_SUCCESS, C_SUCCESS_H,  "#FFFFFF", False),
            "danger":    (C_DANGER,  C_DANGER_H,   "#FFFFFF", False),
            "secondary": (C_ENTRY,   C_DIVIDER,    C_FG,      True),
            "ghost":     (C_BG,      C_DIVIDER,    C_FG2,     False),
        }
        bg, abg, fg, bordered = palettes.get(style, palettes["secondary"])
        opts = dict(
            text=text, command=command,
            bg=bg, fg=fg,
            activebackground=abg, activeforeground=fg,
            disabledforeground=C_FG3,
            relief="flat", bd=0, padx=16, pady=6,
            font=FONT_B, cursor="hand2", state=state,
        )
        if bordered:
            # "Bordered" 스타일 — 옅은 hairline 테두리로 눌러 짐작 가능한 컨트롤임을 표시
            opts.update(highlightthickness=1, highlightbackground=C_BORDER,
                        highlightcolor=C_BORDER)
        if width:
            opts["width"] = width
        opts.update(kw)
        btn = tk.Button(parent, **opts)
        btn._normal_bg = bg
        self._all_btns.append(btn)
        return btn

    # ────────────────────────────── UI 구성 ──────────────────────────────────

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=0)
        self.root.rowconfigure(4, weight=1)

        self._make_zoom_header(row=0)
        self._make_conn_frame(row=1)
        self._make_middle_frame(row=2)
        self._make_period_ctrl_frame(row=3)
        self._make_log_frame(row=4)

    # ── 헤더 (타이틀 + 툴바) ─────────────────────────────────────────────────
    # HIG Layout: "essential information" — 앱 이름을 Large Title로 최상단에 배치해
    # 뷰의 정체성을 즉시 알 수 있게 하고, 부가 컨트롤(배율/버전)은 같은 행 우측에 정렬.

    def _make_zoom_header(self, row):
        frm = ttk.Frame(self.root, style="TFrame")
        frm.grid(row=row, column=0, sticky="ew", padx=22, pady=(18, 8))
        frm.columnconfigure(1, weight=1)

        self.lbl_title = tk.Label(frm, text="국토정보맵 자동 다운로더",
                                   bg=C_BG, fg=C_FG, font=FONT_LARGETITLE)
        self.lbl_title.grid(row=0, column=0, sticky="w")

        toolbar = ttk.Frame(frm, style="TFrame")
        toolbar.grid(row=0, column=1, sticky="e")

        ttk.Label(toolbar, text="화면 배율", style="Bg.TLabel").pack(
            side="left", padx=(0, 6))

        zoom_values = [f"{p}%" for p in range(70, 135, 5)]
        cb = ttk.Combobox(toolbar, textvariable=self.zoom_var,
                          values=zoom_values, state="readonly", width=6)
        cb.pack(side="left")

        ttk.Label(toolbar, text="ver. 0.9", style="Bg.TLabel").pack(
            side="left", padx=(14, 0))

        # 헤더와 본문을 가르는 hairline 구분선 (HIG separatorColor)
        tk.Frame(frm, bg=C_BORDER, height=1).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))

    # ── 배율 변경 콜백 ───────────────────────────────────────────────────────

    def _on_zoom_changed(self, *_):
        global _USER_ZOOM, FONT, FONT_B, FONT_TITLE, FONT_LARGETITLE, FONT_SMALL, FONT_LOG
        try:
            pct = int(self.zoom_var.get().rstrip('%'))
        except ValueError:
            return
        if pct == self._zoom_pct:
            return
        self._zoom_pct = pct
        _USER_ZOOM = pct / 100.0

        # 폰트 전역 재계산
        FONT       = (_FONT_FAMILY, _fs(9))
        FONT_B     = (_FONT_FAMILY, _fs(9),  "bold")
        FONT_TITLE = (_FONT_FAMILY, _fs(11), "bold")
        FONT_LARGETITLE = (_FONT_FAMILY, _fs(15), "bold")
        FONT_SMALL = (_FONT_FAMILY, _fs(8))
        FONT_LOG   = ("Consolas",     _fs(9))

        # ttk 스타일 전체 재적용 (Combobox arrowsize, Progressbar thickness 포함)
        self._setup_theme()

        # tk.Button 폰트 일괄 갱신
        for btn in self._all_btns:
            try:
                btn.configure(font=FONT_B)
            except tk.TclError:
                pass

        # 개별 비-ttk 위젯 갱신
        for attr, fnt in (
            ("lbl_title",    FONT_LARGETITLE),
            ("lbl_status",   FONT_B),
            ("lbl_progress", FONT),
            ("lbl_ok",       FONT_B),
            ("lbl_ng",       FONT_B),
            ("sgg_lb",       FONT),
            ("region_lb",    FONT),
            ("data_lb",      FONT),
            ("log_text",     FONT_LOG),
        ):
            try:
                getattr(self, attr).configure(font=fnt)
            except (AttributeError, tk.TclError):
                pass

        # 창 크기(=최소 크기) 비례 조정
        self._apply_window_size()

    # ── 연결 프레임 ──────────────────────────────────────────────────────────

    def _make_conn_frame(self, row):
        frm = ttk.LabelFrame(self.root, text="Chrome 연결", padding=(16, 12))
        frm.grid(row=row, column=0, sticky="ew", padx=22, pady=(10, 8))
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="CDP URL", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10))
        self.cdp_var = tk.StringVar(value=CDP_URL)
        ttk.Entry(frm, textvariable=self.cdp_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 10))

        self.btn_connect = self._mk_btn(frm, "연결", self.cmd_connect, "primary")
        self.btn_connect.grid(row=0, column=2, padx=(0, 12))

        self.lbl_status = tk.Label(frm, text="● 미연결",
                                   bg=C_CARD, fg=C_ERR, font=FONT_B)
        self.lbl_status.grid(row=0, column=3)

    # ── 중간 프레임 ──────────────────────────────────────────────────────────

    def _make_middle_frame(self, row):
        outer = ttk.Frame(self.root)
        outer.grid(row=row, column=0, sticky="nsew", padx=20, pady=8)
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=2)

        self._make_data_frame(outer, col=0)
        self._make_region_frame(outer, col=1)


    # ── 데이터 설정 프레임 ────────────────────────────────────────────────────

    def _make_data_frame(self, parent, col):
        frm = ttk.LabelFrame(parent, text="데이터 설정", padding=(16, 12))
        frm.grid(row=0, column=col, sticky="nsew", padx=(0, 6))
        frm.columnconfigure(1, weight=1)
        self._data_frm = frm

        # 카테고리 선택 — HIG Segmented Control: 상호 배타적인 소수 옵션(CATEGORIES 목록,
        # 현재 5개)에는
        # Combobox보다 세그먼트 컨트롤이 더 즉각적으로 현재 선택을 드러낸다.
        ttk.Label(frm, text="분류", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        self.cat_var = tk.StringVar(value="건물")
        self._cat_seg_btns = {}
        # 세그먼트 배경을 hairline 구분선 색으로 깔고, 버튼 사이 1px 간격으로 그 색이
        # 비쳐 보이게 해서 선택되지 않은 세그먼트끼리도 경계가 흐릿하게나마 구분되게 한다
        # (이전에는 프레임과 비선택 버튼 배경이 같은 색이라 경계가 전혀 안 보였다).
        seg = ttk.Frame(frm, style="SegBg.TFrame")
        seg.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=(0, 8))
        for i, cat in enumerate(CATEGORIES):
            seg.columnconfigure(i, weight=1)
            b = ttk.Button(seg, text=CATEGORY_LABELS.get(cat, cat), style="SegOff.TButton",
                           command=lambda c=cat: self.cat_var.set(c))
            b.grid(row=0, column=i, sticky="ew", padx=1, pady=1)
            self._cat_seg_btns[cat] = b
        self._refresh_cat_seg()
        self.cat_var.trace_add(
            "write", lambda *_: (self._refresh_cat_seg(), self._refresh_period_enabled()))

        tk.Frame(frm, bg=C_DIVIDER, height=1).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))

        self._chain_frame = ttk.Frame(frm, style="Card.TFrame")
        self._chain_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._chain_frame.columnconfigure(1, weight=1)

        # 체인은 더 이상 카테고리별로 하드코딩하지 않는다 — select_name을 키로 하는
        # 동적 dict로 관리하며, _do_load_all_options()가 실행 시점에 사이트 DOM을
        # 스캔해 실제 존재하는 체인을 구성한다 (자세한 내용은 그 함수 주석 참고).
        self._chain_vars   = {}   # sel_name -> tk.StringVar
        self._chain_traced = set()  # trace_add 중복 방지용 sel_name 집합
        self._combo_info   = {}   # sel_name -> ttk.Combobox
        self._chain_vmaps  = {}   # sel_name -> {표시텍스트: 실제value}
        self._data_chain   = []   # [(label, sel_name), ...] 현재 구성된 체인

        tk.Frame(frm, bg=C_DIVIDER, height=1).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(6, 6))

        # ── 기간 (시작 → 종료) ────────────────────────────────────────────────
        # 기간은 실행 시점의 전역 설정이 아니라 "이 데이터 조합에 붙는 값"이므로
        # 분류·체인과 같은 데이터 설정 패널 안에, [추가 ▼] 바로 위에 둔다.
        ttk.Label(frm, text="기간", style="Muted.TLabel").grid(
            row=4, column=0, sticky="w", pady=3)
        per = ttk.Frame(frm, style="Card.TFrame")
        per.grid(row=4, column=1, sticky="ew", padx=(10, 0), pady=3)
        per.columnconfigure(0, weight=1)
        per.columnconfigure(2, weight=1)
        self.sy_var = tk.StringVar()
        self.sy_cb = ttk.Combobox(per, textvariable=self.sy_var,
                                  values=[], state="readonly", width=10)
        self.sy_cb.grid(row=0, column=0, sticky="ew")
        ttk.Label(per, text="→", style="Muted.TLabel").grid(row=0, column=1, padx=6)
        self.ey_var = tk.StringVar()
        self.ey_cb = ttk.Combobox(per, textvariable=self.ey_var,
                                  values=[], state="readonly", width=10)
        self.ey_cb.grid(row=0, column=2, sticky="ew")
        self._period_options: list = []

        tk.Frame(frm, bg=C_DIVIDER, height=1).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(8, 6))

        # 옵션 불러오기 + 추가 — 지역 선택 패널과 같은 패턴(왼쪽 준비 동작,
        # 오른쪽 목록 담기)으로 맞춰 두 패널의 조작 방식을 통일한다.
        act_row = ttk.Frame(frm, style="Card.TFrame")
        act_row.grid(row=6, column=0, columnspan=2, sticky="ew")
        act_row.columnconfigure(0, weight=1)
        self._mk_btn(act_row, "옵션 불러오기", self.cmd_load_data_options,
                     "secondary").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._mk_btn(act_row, "추가 ▼", self.cmd_add_data,
                     "primary").grid(row=0, column=1, sticky="e")

        tk.Frame(frm, bg=C_DIVIDER, height=1).grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(8, 6))

        # ── 작업 데이터 목록 ─────────────────────────────────────────────────
        # 데이터 설정을 하나만 쓰지 않고 여러 조합(분류·체인·기간)을 쌓아 두었다가
        # 한 번의 실행으로 순서대로 처리한다. 기간은 항목마다 따로 저장되므로
        # 자료별로 제공 기간이 달라도 안전하다.
        dhdr = ttk.Frame(frm, style="Card.TFrame")
        dhdr.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        ttk.Label(dhdr, text="작업 데이터 목록", style="Muted.TLabel").pack(side="left")
        self.data_count_lbl = ttk.Label(dhdr, text="0개", style="Small.TLabel")
        self.data_count_lbl.pack(side="left", padx=(6, 0))

        data_box = ttk.Frame(frm, style="Card.TFrame")
        data_box.grid(row=9, column=0, columnspan=2, sticky="nsew")
        data_box.columnconfigure(0, weight=1)
        data_box.rowconfigure(0, weight=1)
        frm.rowconfigure(9, weight=1)
        self.data_lb = tk.Listbox(
            data_box, selectmode=tk.EXTENDED, height=DATA_LINES, exportselection=False,
            bg=C_ENTRY, fg=C_FG, font=FONT,
            selectbackground=C_SEL, selectforeground=C_FG,
            activestyle="none", relief="flat", bd=0,
            highlightthickness=1, highlightcolor=C_ACC, highlightbackground=C_BORDER)
        data_scroll = ttk.Scrollbar(data_box, orient="vertical",
                                    command=self.data_lb.yview)
        data_xscroll = ttk.Scrollbar(data_box, orient="horizontal",
                                     command=self.data_lb.xview)
        self.data_lb.configure(yscrollcommand=data_scroll.set,
                               xscrollcommand=data_xscroll.set)
        self.data_lb.grid(row=0, column=0, sticky="nsew")
        data_scroll.grid(row=0, column=1, sticky="ns")
        # 항목 문자열이 길어 잘리는 일이 잦아 가로 스크롤도 붙였다
        data_xscroll.grid(row=1, column=0, sticky="ew")

        data_btn_row = ttk.Frame(frm, style="Card.TFrame")
        data_btn_row.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._mk_btn(data_btn_row, "전체선택",
                     lambda: self.data_lb.selection_set(0, tk.END),
                     "secondary").pack(side="left", padx=(0, 4))
        self._mk_btn(data_btn_row, "전체해제",
                     lambda: self.data_lb.selection_clear(0, tk.END),
                     "secondary").pack(side="left")
        self._mk_btn(data_btn_row, "제거", self.cmd_remove_data,
                     "secondary").pack(side="right")

    def _refresh_cat_seg(self):
        """카테고리 세그먼트 컨트롤의 선택 상태(강조색 vs 중립색)를 갱신."""
        current = self.cat_var.get()
        for cat, b in self._cat_seg_btns.items():
            try:
                b.configure(style="SegOn.TButton" if cat == current else "SegOff.TButton")
            except tk.TclError:
                pass

    def _category_supports_run(self) -> bool:
        """현재 카테고리가 '기간 및 실행'(범위 다운로드)을 지원하는지.
        국토모니터링처럼 기준년도가 범위가 아니라 체인 필드로 단일 선택되는
        카테고리는 이 도구의 기간 반복 다운로드 방식과 맞지 않아 실행을 막는다."""
        return self.cat_var.get() not in CHAIN_INCLUDE_YEAR_CATEGORIES

    def _refresh_period_enabled(self):
        """카테고리 전환에 따라 '기간 및 실행' 입력을 켜고 끈다."""
        if not hasattr(self, "sy_cb"):
            return   # 아직 기간 프레임이 만들어지기 전(초기 구성 중)
        enabled = self._category_supports_run()
        combo_state = "readonly" if enabled else "disabled"
        self.sy_cb.configure(state=combo_state)
        self.ey_cb.configure(state=combo_state)
        if not enabled:
            self.sy_var.set("")
            self.ey_var.set("")
        self._set_run_state(self.is_running)

    def _get_chain_var(self, sel_name: str) -> tk.StringVar:
        """select_name에 대응하는 StringVar을 가져오거나 새로 만든다.
        새로 만드는 경우 값 변경 시 하위 체인이 자동 갱신되도록 trace를 건다."""
        var = self._chain_vars.get(sel_name)
        if var is None:
            var = tk.StringVar()
            self._chain_vars[sel_name] = var
        if sel_name not in self._chain_traced:
            self._chain_traced.add(sel_name)
            var.trace_add("write",
                           lambda *_ , sn=sel_name: self._run_bg(self._on_chain_changed(sn)))
        return var

    def _apply_chain(self, chain: list):
        """chain: [(label, select_name), ...] — 체인 콤보 재구성.
        기존에 이미 만들어둔 행은 값을 보존한 채 재사용하고, 새로 추가된 select만
        새 행으로 만든다 (자동 탐지 도중 체인이 점점 늘어나는 경우를 위해)."""
        self._data_chain = chain
        for w in self._chain_frame.winfo_children():
            w.destroy()
        self._combo_info = {}
        for i, (label, sel_name) in enumerate(chain):
            ttk.Label(self._chain_frame, text=label, style="Muted.TLabel").grid(
                row=i, column=0, sticky="w", pady=3)
            var = self._get_chain_var(sel_name)
            # 체인이 늘어날 때마다 이 함수가 반복 호출되어 모든 행을 새로 만든다.
            # 이미 값을 알고 있는 필드(=_chain_vmaps에 존재)는 빈 리스트가 아니라
            # 기존에 발견된 옵션 목록으로 즉시 채워야, 나중 필드가 발견될 때마다
            # 앞 필드들의 드롭다운 목록이 빈 채로 덮어써지는 문제가 없다.
            known_values = list(self._chain_vmaps.get(sel_name, {}).keys())
            cb = ttk.Combobox(self._chain_frame, textvariable=var,
                              values=known_values, state="readonly")
            cb.grid(row=i, column=1, sticky="ew", padx=(10, 0), pady=3)
            self._combo_info[sel_name] = cb

    # ── 지역 선택 프레임 ─────────────────────────────────────────────────────

    def _make_region_frame(self, parent, col):
        frm = ttk.LabelFrame(parent, text="지역 선택", padding=(16, 12))
        frm.grid(row=0, column=col, sticky="nsew", padx=(6, 0))
        frm.columnconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(2, weight=1)
        frm.rowconfigure(6, weight=1)

        self.sido_var = tk.StringVar()
        self.sido_cb = ttk.Combobox(frm, textvariable=self.sido_var,
                                    values=SIDO_LIST, state="readonly")
        self.sido_cb.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.sido_var.trace_add("write", lambda *_: self.cmd_load_sgg())

        ttk.Label(frm, text="시군구", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 3))
        ttk.Label(frm, text="Ctrl+클릭 복수 선택", style="Small.TLabel").grid(
            row=1, column=1, sticky="e", pady=(0, 3))

        sgg_box = ttk.Frame(frm, style="Card.TFrame")
        sgg_box.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(0, 6))
        sgg_box.columnconfigure(0, weight=1)
        self.sgg_lb = tk.Listbox(
            sgg_box, selectmode=tk.EXTENDED, height=SGG_LINES, exportselection=False,
            bg=C_ENTRY, fg=C_FG, font=FONT,
            selectbackground=C_SEL, selectforeground=C_FG,
            activestyle="none", relief="flat", bd=0,
            highlightthickness=1, highlightcolor=C_ACC, highlightbackground=C_BORDER)
        sgg_scroll = ttk.Scrollbar(sgg_box, orient="vertical", command=self.sgg_lb.yview)
        self.sgg_lb.configure(yscrollcommand=sgg_scroll.set)
        self.sgg_lb.grid(row=0, column=0, sticky="nsew")
        sgg_scroll.grid(row=0, column=1, sticky="ns")

        btn_row = ttk.Frame(frm, style="Card.TFrame")
        btn_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._mk_btn(btn_row, "전체선택",
                     lambda: self.sgg_lb.selection_set(0, tk.END),
                     "secondary").pack(side="left", padx=(0, 4))
        self._mk_btn(btn_row, "전체해제",
                     lambda: self.sgg_lb.selection_clear(0, tk.END),
                     "secondary").pack(side="left")
        self._mk_btn(btn_row, "추가 ▶", self.cmd_add_region,
                     "primary").pack(side="right")

        tk.Frame(frm, bg=C_DIVIDER, height=1).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(0, 6))

        hdr = ttk.Frame(frm, style="Card.TFrame")
        hdr.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        ttk.Label(hdr, text="작업 지역 목록", style="Muted.TLabel").pack(side="left")
        self.region_count_lbl = ttk.Label(hdr, text="0개", style="Small.TLabel")
        self.region_count_lbl.pack(side="left", padx=(6, 0))

        reg_box = ttk.Frame(frm, style="Card.TFrame")
        reg_box.grid(row=6, column=0, columnspan=2, sticky="nsew")
        reg_box.columnconfigure(0, weight=1)
        self.region_lb = tk.Listbox(
            reg_box, selectmode=tk.EXTENDED, height=REGION_LINES, exportselection=False,
            bg=C_ENTRY, fg=C_FG, font=FONT,
            selectbackground=C_SEL, selectforeground=C_FG,
            activestyle="none", relief="flat", bd=0,
            highlightthickness=1, highlightcolor=C_ACC, highlightbackground=C_BORDER)
        reg_scroll = ttk.Scrollbar(reg_box, orient="vertical", command=self.region_lb.yview)
        self.region_lb.configure(yscrollcommand=reg_scroll.set)
        self.region_lb.grid(row=0, column=0, sticky="nsew")
        reg_scroll.grid(row=0, column=1, sticky="ns")

        # 시군구 목록과 동일한 패턴 — 전체선택/전체해제는 왼쪽, 제거(파괴적 동작)는
        # 오른쪽. 이전에는 "제거"만 목록 위 헤더에 단독으로 있어 성격이 다른
        # 전체선택/전체해제와 짝이 안 맞았다.
        reg_btn_row = ttk.Frame(frm, style="Card.TFrame")
        reg_btn_row.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._mk_btn(reg_btn_row, "전체선택",
                     lambda: self.region_lb.selection_set(0, tk.END),
                     "secondary").pack(side="left", padx=(0, 4))
        self._mk_btn(reg_btn_row, "전체해제",
                     lambda: self.region_lb.selection_clear(0, tk.END),
                     "secondary").pack(side="left")
        self._mk_btn(reg_btn_row, "제거", self.cmd_remove_region,
                     "secondary").pack(side="right")

    # ── 실행 프레임 ──────────────────────────────────────────────────────────

    def _make_period_ctrl_frame(self, row):
        """실행 프레임 — 왼쪽부터 진행 카운터·진행 바, 그다음 성공/실패 집계,
        오른쪽 끝에 중지/실행 버튼."""
        frm = ttk.LabelFrame(self.root, text="실행", padding=(16, 12))
        frm.grid(row=row, column=0, sticky="ew", padx=22, pady=8)
        frm.columnconfigure(1, weight=1)      # 진행 바가 남는 폭을 모두 차지

        self.lbl_progress = tk.Label(frm, text="0 / 0", anchor="w",
                                     bg=C_CARD, fg=C_FG2, font=FONT, width=9)
        self.lbl_progress.grid(row=0, column=0, sticky="w", padx=(0, 10))

        self.progress_var = tk.DoubleVar()
        ttk.Progressbar(frm, variable=self.progress_var, maximum=100).grid(
            row=0, column=1, sticky="ew", padx=(0, 18))

        # 진행 바와 버튼 사이 — 실행 중 계속 갱신되는 성공/실패 집계
        stat = ttk.Frame(frm, style="Card.TFrame")
        stat.grid(row=0, column=2, sticky="e", padx=(0, 18))
        self.lbl_ok = tk.Label(stat, text="성공 0", bg=C_CARD, fg=C_OK, font=FONT_B)
        self.lbl_ok.pack(side="left", padx=(0, 12))
        self.lbl_ng = tk.Label(stat, text="실패 0", bg=C_CARD, fg=C_FG3, font=FONT_B)
        self.lbl_ng.pack(side="left")

        btns = ttk.Frame(frm, style="Card.TFrame")
        btns.grid(row=0, column=3, sticky="e")
        self.btn_stop = self._mk_btn(btns, "■  중지", self.cmd_stop,
                                     "danger", state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 4))
        self.btn_run = self._mk_btn(btns, "▶  실행", self.cmd_run,
                                    "success", state="disabled")
        self.btn_run.pack(side="left")

    def _set_stats(self, ok: int, ng: int):
        """성공/실패 집계 갱신. 실패가 0이면 회색으로 눌러 두고, 1건이라도 생기면
        경고색으로 바꿔 눈에 띄게 한다."""
        try:
            self.lbl_ok.configure(text=f"성공 {ok}")
            self.lbl_ng.configure(text=f"실패 {ng}", fg=C_ERR if ng else C_FG3)
        except (AttributeError, tk.TclError):
            pass

    def _update_period_combos(self, opts: list):
        """year_text_area 옵션으로 기간 콤보박스 갱신."""
        self._period_options = opts
        self.sy_cb["values"] = opts
        self.ey_cb["values"] = opts
        if opts:
            self.sy_var.set(opts[0])
            self.ey_var.set(opts[0])

    def _get_period_range(self) -> list:
        """시작~종료 사이 period 목록 반환."""
        sy = self.sy_var.get()
        ey = self.ey_var.get()
        opts = self._period_options
        if not opts:
            return []
        try:
            si = opts.index(sy)
            ei = opts.index(ey)
            lo, hi = min(si, ei), max(si, ei)
            # opts 는 최신→과거 순 → reversed 로 과거→최신(오름차순) 반환
            return list(reversed(opts[lo:hi + 1]))
        except ValueError:
            return list(reversed(opts))

    # ── 로그 프레임 ───────────────────────────────────────────────────────────

    def _make_log_frame(self, row):
        frm = ttk.LabelFrame(self.root, text="실행 로그", padding=(16, 12))
        frm.grid(row=row, column=0, sticky="nsew", padx=22, pady=(8, 18))
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(0, weight=1)
        self.root.rowconfigure(row, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            frm, height=LOG_LINES, wrap="word", state="disabled",
            font=FONT_LOG,
            bg=C_CARD, fg=C_FG,
            insertbackground=C_FG,
            selectbackground=C_SEL, selectforeground=C_FG,
            relief="flat", bd=0,
            highlightthickness=1,
            highlightcolor=C_ACC, highlightbackground=C_BORDER)
        self.log_text.grid(row=0, column=0, sticky="nsew", pady=(0, 6))

        self._mk_btn(frm, "지우기", self._clear_log,
                     "ghost").grid(row=1, column=0, sticky="e")

    # ────────────────────────────── 헬퍼 ─────────────────────────────────────

    def log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _start_loop_thread(self):
        """Playwright 객체를 공유하는 단일 영속 이벤트 루프를 백그라운드 스레드에서 시작"""
        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.run_forever()
        t = threading.Thread(target=run_loop, daemon=True)
        t.start()
        # 루프가 준비될 때까지 대기
        while self._loop is None:
            time.sleep(0.01)

    def _run_bg(self, coro):
        """영속 이벤트 루프에 코루틴 제출 (Playwright 객체 재사용 가능).
        예외가 나면 기존에는 Future 안에 갇혀 아무 흔적 없이 사라졌다 —
        완료 콜백에서 결과를 확인해 실패 시 로그창에 traceback을 남긴다."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _report_exception(f):
            try:
                f.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                import traceback
                self.log("❌ 내부 오류가 발생했습니다:\n" + traceback.format_exc())

        fut.add_done_callback(_report_exception)

    def _update_combo(self, sel_name: str, values: list, default: str = ""):
        if sel_name not in self._combo_info:
            return
        cb = self._combo_info[sel_name]
        cb["values"] = values
        var = self._get_chain_var(sel_name)
        if values:
            var.set(default if default in values else values[0])
        else:
            var.set("")

    def _set_run_state(self, running: bool):
        self.is_running = running
        # 이제 실행 대상은 화면에 보이는 현재 선택이 아니라 "작업 데이터 목록"이다.
        # 따라서 목록이 비어 있으면 실행할 수 없고, 목록에 무언가 담겨 있으면
        # (현재 화면의 분류가 국토모니터링이더라도) 실행할 수 있다.
        can_run = (not running) and bool(self._data_items)
        self.btn_run.configure(
            state="normal" if can_run else "disabled",
            bg=C_SUCCESS if can_run else C_DIVIDER)
        self.btn_stop.configure(
            state="normal" if running else "disabled",
            bg=C_DANGER if running else C_DIVIDER)

    # ────────────────────────────── 명령 ─────────────────────────────────────

    def cmd_connect(self):
        self._run_bg(self._async_connect())

    def cmd_load_data_options(self):
        if not self.map_page:
            messagebox.showwarning("경고", "먼저 Chrome에 연결하세요.")
            return
        self._run_bg(self._async_load_all_options())

    def cmd_load_sgg(self):
        if not self.map_page or not self._panel_sel:
            self.sgg_lb.delete(0, tk.END)
            return
        self._run_bg(self._async_load_sgg())

    def cmd_add_region(self):
        sido = self.sido_var.get().strip()
        if not sido:
            return
        selected = self.sgg_lb.curselection()
        if not selected:
            messagebox.showinfo("알림", "시군구를 선택하세요.")
            return
        existing = set(self.region_lb.get(0, tk.END))
        for idx in selected:
            sgg = self.sgg_lb.get(idx).strip()
            entry = f"{sido} {sgg}"
            if entry not in existing:
                self.region_lb.insert(tk.END, entry)
                existing.add(entry)
        self._update_region_count()

    def cmd_remove_region(self):
        for idx in reversed(self.region_lb.curselection()):
            self.region_lb.delete(idx)
        self._update_region_count()

    def _update_region_count(self):
        self.region_count_lbl.configure(text=f"{self.region_lb.size()}개")

    # ── 작업 데이터 목록 ─────────────────────────────────────────────────

    def _snapshot_data_item(self) -> dict | None:
        """현재 "데이터 설정" 화면의 선택을 그대로 굳혀 하나의 작업 항목으로 만든다.
        기간까지 항목에 함께 담기 때문에, 자료마다 제공 기간이 달라도 나중에
        실행할 때 서로 간섭하지 않는다. 선택이 불완전하면 None을 반환한다."""
        if not self._data_chain:
            messagebox.showwarning("경고", "먼저 [옵션 불러오기]로 데이터 항목을 불러오세요.")
            return None
        if self._chain_category != self.cat_var.get():
            messagebox.showwarning(
                "경고",
                f"현재 화면의 데이터 항목은 '{self._chain_category}' 분류에서 불러온 것입니다.\n"
                f"'{self.cat_var.get()}'로 바꾼 뒤에는 [옵션 불러오기]를 다시 실행하세요.")
            return None

        chain_vals = {sn: self._get_chain_var(sn).get().strip()
                      for _, sn in self._data_chain}
        missing = [label for label, sn in self._data_chain if not chain_vals[sn]]
        if missing:
            messagebox.showwarning("경고", f"선택되지 않은 항목이 있습니다: {', '.join(missing)}")
            return None

        cat = self.cat_var.get()
        if self._category_supports_run():
            periods = self._get_period_range()
            if not periods:
                messagebox.showwarning("경고", "기간을 먼저 선택하세요.")
                return None
            per_txt = (f"{periods[0]}~{periods[-1]} ({len(periods)}건)"
                       if len(periods) > 1 else f"{periods[0]} (1건)")
        else:
            # 국토모니터링처럼 기준년도가 체인 안에서 단일 선택되는 카테고리는
            # 기간 반복이 없다 — 빈 목록으로 두고 실행 시 1건으로 처리한다.
            periods = []
            per_txt = "기간 없음"

        chain_txt = " › ".join(chain_vals[sn] for _, sn in self._data_chain)
        return {
            "category":    cat,
            "chain":       list(self._data_chain),
            "chain_vals":  chain_vals,
            "chain_vmaps": {k: dict(v) for k, v in self._chain_vmaps.items()},
            "periods":     periods,
            "label":       f"[{cat}] {chain_txt}  |  {per_txt}",
        }

    @staticmethod
    def _data_item_key(item: dict):
        """중복 추가 방지용 비교 키 (분류 + 선택값 + 기간)."""
        return (item["category"],
                tuple(sorted(item["chain_vals"].items())),
                tuple(item["periods"]))

    def cmd_add_data(self):
        item = self._snapshot_data_item()
        if item is None:
            return
        key = self._data_item_key(item)
        if any(self._data_item_key(x) == key for x in self._data_items):
            messagebox.showinfo("알림", "이미 목록에 있는 데이터 설정입니다.")
            return
        self._data_items.append(item)
        self.data_lb.insert(tk.END, item["label"])
        self.data_lb.see(tk.END)
        self._update_data_count()

    def cmd_remove_data(self):
        for idx in reversed(self.data_lb.curselection()):
            self.data_lb.delete(idx)
            del self._data_items[idx]
        self._update_data_count()

    def _update_data_count(self):
        self.data_count_lbl.configure(text=f"{len(self._data_items)}개")
        self._set_run_state(self.is_running)

    # ── 실행 ─────────────────────────────────────────────────────────────

    def cmd_run(self):
        if not self.map_page:
            messagebox.showwarning("경고", "먼저 Chrome에 연결하세요.")
            return
        regions = list(self.region_lb.get(0, tk.END))
        if not regions:
            messagebox.showwarning("경고", "작업 지역을 추가하세요.")
            return
        if not self._data_items:
            messagebox.showwarning("경고", "[추가 ▼]로 작업 데이터를 목록에 담으세요.")
            return

        cfg = {
            "items":   [dict(it) for it in self._data_items],
            "regions": regions,
        }
        self.stop_event.clear()
        self.root.after(0, lambda: self._set_run_state(True))
        self._run_bg(self._async_run(cfg))

    def cmd_stop(self):
        self.stop_event.set()
        self.log("⏹ 중지 요청됨...")

    # ────────────────────────────── 비동기 함수 ──────────────────────────────

    async def _async_connect(self):
        if not HAS_PLAYWRIGHT:
            self.log("❌ playwright 미설치: pip install playwright && playwright install chromium")
            return

        # 입력값을 정규화한다 — 포트만 적거나 스킴을 빼먹어도 동작하게.
        host, port, cdp_url = parse_cdp_url(self.cdp_var.get())
        self.root.after(0, lambda u=cdp_url: self.cdp_var.set(u))
        urls = cdp_candidates(host, port)

        pw = await async_playwright().start()

        async def _try_connect(timeout_ms: int = 5000):
            """후보 URL들을 차례로 시도해 첫 성공한 browser를 반환 (없으면 None).

            timeout을 반드시 준다 — 포트에 HTTP로 응답하지 않는 프로그램이 물려 있으면
            기본값으로는 수십 초씩 멈춰 있어 "눌러도 아무 반응이 없는" 것처럼 보인다."""
            for u in urls:
                try:
                    b = await pw.chromium.connect_over_cdp(u, timeout=timeout_ms)
                    self.log(f"↳ CDP 연결 성공: {u}")
                    return b
                except Exception:
                    continue
            return None

        # ── 1단계: 그 포트가 이미 열려 있으면 기존 Chrome에 붙는다 ─────────────
        self.log(f"🔌 기존 Chrome 연결 시도: {cdp_url}")
        browser = None
        if is_port_open(host, port):
            browser = await _try_connect(timeout_ms=5000)
            if browser is None:
                # 포트는 열려 있는데 CDP 연결이 안 된다 = 그 포트를 다른 프로그램이
                # 쓰고 있다는 뜻. 이 상태로 Chrome을 띄우면 Chrome이 디버깅 포트를
                # 바인딩하지 못해 "창은 뜨는데 연결은 안 되는" 상황이 반복된다.
                ver = await asyncio.to_thread(probe_cdp_version, host, port)
                if ver:
                    self.log(f"❌ 포트 {port}에서 Chrome DevTools({ver})가 응답하지만 "
                             f"연결에 실패했습니다. 다른 자동화 도구가 이미 붙어 있는지 "
                             f"확인하거나, Chrome을 닫고 다시 시도하세요.")
                else:
                    free = find_free_port(max(9223, port + 1))
                    self.log(f"❌ 포트 {port}는 이미 다른 프로그램이 쓰고 있습니다 "
                             f"(CDP 엔드포인트가 아님).")
                    self.log(f"   → 이 포트로는 Chrome이 디버깅 포트를 열 수 없습니다. "
                             f"CDP URL을 비어 있는 포트로 바꾸고 다시 [연결]을 누르세요."
                             + (f"  (예: http://localhost:{free})" if free else ""))
                await pw.stop()
                return

        # ── 2단계: 포트가 닫혀 있으면 그 포트로 Chrome을 새로 띄운다 ───────────
        if browser is None:
            chrome = find_chrome()
            if not chrome:
                self.log("❌ Chrome을 찾을 수 없습니다. Chrome을 설치하거나 직접 실행 후 다시 시도하세요.")
                await pw.stop()
                return

            # 임시 프로필 디렉토리 (기존 Chrome 프로필과 충돌 방지).
            # 프로필을 분리하지 않으면, 이미 실행 중인 Chrome이 새 창만 열어주고
            # --remote-debugging-port 는 무시된다.
            tmp_dir = tempfile.mkdtemp(prefix="ngii_chrome_")
            self.log(f"🚀 Chrome 실행 중 (디버깅 포트 {port}): {chrome}")
            try:
                subprocess.Popen([
                    chrome,
                    f"--remote-debugging-port={port}",   # ← 입력창의 포트를 그대로 사용
                    "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={tmp_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-popup-blocking",
                    MAP_URL,
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                self.log(f"❌ Chrome 실행 실패: {e}")
                await pw.stop()
                return

            # Chrome이 CDP 포트를 열 때까지 최대 15초 대기
            self.log("⏳ Chrome 시작 대기 중...")
            for _ in range(30):
                await asyncio.sleep(0.5)
                if not is_port_open(host, port, timeout=0.2):
                    continue      # 아직 포트가 안 열림 — 연결 시도 자체를 아낀다
                browser = await _try_connect(timeout_ms=3000)
                if browser is not None:
                    break

            if browser is None:
                await pw.stop()
                self.log(f"❌ Chrome을 띄웠지만 포트 {port}에 연결하지 못했습니다.")
                if is_port_open(host, port):
                    self.log("   포트는 열려 있으나 CDP 응답이 없습니다 — 방화벽/보안 프로그램이 "
                             "로컬 연결을 막고 있는지 확인하세요.")
                else:
                    self.log("   포트가 열리지 않았습니다 — Chrome이 그 포트를 바인딩하지 못했다는 뜻입니다. "
                             "다른 포트로 바꾸거나, 열려 있는 Chrome을 모두 닫고 다시 시도하세요.")
                self.log(f"   수동 실행: chrome.exe --remote-debugging-port={port} "
                         f"--user-data-dir=%TEMP%\\ngii_chrome")
                return

        # ── 3단계: 국토정보맵 탭 탐색 / 없으면 새 탭으로 이동 ──────────────────
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = next((p for p in ctx.pages if MAP_URL_KEYWORD in p.url), None)
        if page is None:
            if ctx.pages:
                page = ctx.pages[0]
                await page.goto(MAP_URL, wait_until="domcontentloaded")
            else:
                page = await ctx.new_page()
                await page.goto(MAP_URL, wait_until="domcontentloaded")
            self.log(f"↳ 국토정보맵으로 이동: {MAP_URL}")

        self.playwright_obj = pw
        self.browser = browser
        self.map_page = page
        self.root.after(0, lambda: (
            self.lbl_status.configure(text="● 연결됨", fg=C_OK),
            self._set_run_state(False),
            self.log(f"✅ 연결 성공: {page.url}"),
        ))

    async def _async_load_all_options(self):
        """사이트 DOM을 실시간 탐지해 데이터 체인 + 기간 옵션을 자동 구성한다."""
        self._loading_options = True
        try:
            await self._do_load_all_options()
        finally:
            self._loading_options = False

    def _dedupe_label(self, label: str) -> str:
        """같은 표시 텍스트(예: '자료유형 선택')가 체인 안에 이미 있으면 구분용 번호를 붙인다."""
        label = (label or "선택").strip() or "선택"
        existing = {l for l, _ in self._data_chain}
        if label not in existing:
            return label
        n = 2
        while f"{label} ({n})" in existing:
            n += 1
        return f"{label} ({n})"

    async def _discover_chain_steps(self, page, panel_sel, chain_timeout, processed: set,
                                     category: str = None) -> bool:
        """패널 DOM을 반복적으로 다시 스캔하면서, 아직 처리하지 않은 체인 select를
        발견되는 순서대로 하나씩 값을 채워 넣는다 (self._data_chain / self._chain_vmaps 갱신).

        사이트가 select를 처음부터 다 그려두는 경우(예: 국토지표)와, 이전 단계에
        값을 넣어야 다음 select가 DOM에 나타나는 경우(예: 인구/건물/토지)를 모두
        같은 루프로 처리한다 — 매 단계마다 다시 스캔하기 때문에 카테고리별로
        체인을 하드코딩할 필요가 없다.

        더 이상 새로운 후보가 없으면 True, 중간에 타임아웃으로 중단되면 False 반환.
        """
        while True:
            scan = await get_panel_select_names(page, panel_sel)
            next_item = None
            for name, placeholder in scan:
                if not is_chain_candidate(name, category) or name in processed:
                    continue
                next_item = (name, placeholder)
                break
            if next_item is None:
                return True

            sel_name, placeholder = next_item
            # 대기 + 옵션읽기 + 첫값 설정(change 이벤트 포함, 다음 select 유발)을
            # CDP 왕복 1회로 처리 — 필드당 왕복 횟수를 크게 줄여 체감 속도를 높인다.
            opts_tv = await wait_read_and_advance(
                page, panel_sel, sel_name, min_count=1, timeout_sec=chain_timeout, advance=True)
            if opts_tv is None:
                self.log(f"  ⚠ {placeholder or sel_name} ({sel_name}) 로드 대기 시간 초과 "
                          f"— 스캔된 select: {[n for n, _ in scan]}")
                processed.add(sel_name)
                return False

            texts  = [o["t"] if o["t"] else o["v"] for o in opts_tv]
            values = [o["v"] for o in opts_tv]
            label  = self._dedupe_label(placeholder or sel_name)

            self._data_chain.append((label, sel_name))
            self._chain_vmaps[sel_name] = dict(zip(texts, values))
            processed.add(sel_name)
            if sel_name == "key_text_area":
                self._key_map = self._chain_vmaps[sel_name]
            elif sel_name == "itemKey_text_area":
                self._ik_map  = self._chain_vmaps[sel_name]

            self.log(f"  [발견] {label} ({sel_name}): {texts}")
            chain_snapshot = list(self._data_chain)
            self.root.after(0, lambda c=chain_snapshot: self._apply_chain(c))
            self.root.after(0, lambda v=sel_name, t=texts: self._update_combo(v, t))

    async def _do_load_all_options(self):
        page = self.map_page
        cat  = self.cat_var.get()
        chain_timeout = 60 if cat == "국토지표" else 15
        self.log(f"🔄 [{cat}] 옵션 불러오는 중 (사이트 구조 자동 탐지)...")

        tab_res = await activate_category_tab(page, cat)
        self.log(f"  탭 활성화: {tab_res}")
        if tab_res.startswith("ok:"):
            await asyncio.sleep(0.2)   # 실제로 클릭해 탭을 전환한 경우에만 리렌더 대기

        panel_sel = await find_panel_sel(page, cat)
        self._panel_sel = panel_sel
        self.log(f"  패널: {panel_sel}")
        if not panel_sel:
            self.log("  ❌ 패널을 찾지 못했습니다.")
            return

        # 패널 안에 select가 하나라도 실제로 나타날 때까지 대기 (진단을 명확히 하기 위해
        # 첫 필드 로드 대기와는 별도로 짧게 확인)
        panel_ready = await wait_for_element_js(
            page, f"document.querySelector('{panel_sel} select[name]')", timeout_sec=5)
        if not panel_ready:
            self.log("  ⚠ 패널 내 select 요소가 나타나지 않습니다 — 사이트가 아직 렌더링 중일 수 있습니다.")

        self._data_chain  = []
        self._chain_vmaps = {}
        self._chain_category = cat
        self.root.after(0, lambda: self._apply_chain([]))

        period_ok = await self._discover_chain_steps(page, panel_sel, chain_timeout, set(), cat)
        if not self._data_chain:
            self.log("  ⚠ 데이터 체인을 하나도 찾지 못했습니다 — 사이트 구조가 크게 바뀌었을 수 있습니다.")

        if cat in CHAIN_INCLUDE_YEAR_CATEGORIES:
            # 이 카테고리는 기준년도가 위 체인에서 이미 단일 값으로 선택됐고, 다운로드
            # 기간 "범위" 개념 자체가 없어 하단 기간 및 실행 입력을 비활성화해둔다
            # (cat_var 트레이스 → _refresh_period_enabled 가 처리).
            self.log("  ℹ 이 카테고리는 기준년도를 데이터 설정에서 직접 선택합니다 "
                     "(기간 및 실행은 비활성화됩니다).")
        else:
            # 체인 끝난 후 year_text_area(기간) 로드 — 항상 시도 (중간에 타임아웃이 있었어도)
            # (대기 + 읽기를 왕복 1회로 처리, 값은 변경하지 않음)
            year_tv = await wait_read_and_advance(
                page, panel_sel, "year_text_area", min_count=1, timeout_sec=chain_timeout, advance=False)
            if year_tv is None:
                self.log("  ⚠ 기간 옵션 로드 시간 초과")
            else:
                period_opts = [o["v"] for o in year_tv]
                self.log(f"  기간: {len(period_opts)}개 ({period_opts[0] if period_opts else '-'} ~ "
                         f"{period_opts[-1] if period_opts else '-'})")
                self.root.after(0, lambda o=period_opts: self._update_period_combos(o))

        self.log("✅ 옵션 로드 완료" if period_ok else "⚠ 옵션 로드 일부 시간 초과 (로그 확인)")

    async def _on_chain_changed(self, changed_sel_name: str):
        """체인 내 select 값이 바뀌면, 하위 필드들의 '실제 옵션 값'만 그 자리에서
        새로 반영한다 (사이트가 그 값에 따라 하위 select의 선택지를 바꾸는 건
        피할 수 없는 실제 동작이라 재확인 자체는 필요하지만, 화면의 하위 행을
        먼저 통째로 지웠다가 하나씩 다시 그리지는 않는다 — 이전에는 필드가 바뀔
        때마다 하위 행들이 사라졌다 다시 나타나길 반복해 '계속 새로 불러오는'
        것처럼 보였다). 완전히 새로운 필드가 나타나는 경우에만 UI에 행이
        추가된다."""
        if self._loading_options:
            return   # 옵션 불러오기 / 다른 cascade 중에는 간섭 방지
        page = self.map_page
        if not page or not self._panel_sel:
            return
        panel_sel = self._panel_sel
        chain = self._data_chain

        idx = next((i for i, (_, sn) in enumerate(chain) if sn == changed_sel_name), -1)
        if idx == -1:
            return

        var = self._chain_vars.get(changed_sel_name)
        display_val = (var.get().strip() if var else "")
        if not display_val:
            return

        downstream = chain[idx + 1:]   # (화면에서 지우지 않고 그대로 둔 채 값만 갱신)

        self._loading_options = True
        try:
            cat = self.cat_var.get()
            chain_timeout = 60 if cat == "국토지표" else 15

            actual_val = self._chain_vmaps.get(changed_sel_name, {}).get(display_val, display_val)
            await jq_set(page, panel_sel, changed_sel_name, actual_val)

            for label, sname in downstream:
                opts_tv = await wait_read_and_advance(
                    page, panel_sel, sname, min_count=1, timeout_sec=chain_timeout, advance=True)
                if opts_tv is None:
                    self.log(f"  ⚠ [{changed_sel_name} 변경] {label} ({sname}) 로드 시간 초과")
                    break
                texts  = [o["t"] if o["t"] else o["v"] for o in opts_tv]
                values = [o["v"] for o in opts_tv]
                self._chain_vmaps[sname] = dict(zip(texts, values))
                self.root.after(0, lambda v=sname, t=texts: self._update_combo(v, t))

            # 하위 체인 자체가 늘어났는지(사이트에 이전엔 없던 select가 새로 나타났는지)는
            # 계속 자동 탐지 — processed에 기존 필드를 모두 넣어두면 새 필드가 없는 한
            # 화면을 건드리지 않고 즉시 반환된다.
            processed = {sn for _, sn in chain}
            await self._discover_chain_steps(page, panel_sel, chain_timeout, processed, cat)

            if cat not in CHAIN_INCLUDE_YEAR_CATEGORIES:
                # 기간(year_text_area) 자동 갱신 (대기 + 읽기 왕복 1회) — 이 카테고리에서는
                # year_text_area가 이미 체인 필드로 처리되므로 별도 기간 범위 갱신이 불필요.
                year_tv = await wait_read_and_advance(
                    page, panel_sel, "year_text_area", min_count=1, timeout_sec=chain_timeout, advance=False)
                if year_tv is None:
                    self.log(f"  ⚠ [{changed_sel_name} 변경] 기간 로드 시간 초과")
                else:
                    period_opts = [o["v"] for o in year_tv]
                    self.log(f"  기간 갱신: {len(period_opts)}개")
                    self.root.after(0, lambda o=period_opts: self._update_period_combos(o))
        finally:
            self._loading_options = False

    async def _async_load_sgg(self):
        """시도 선택 후 시군구 목록 로드"""
        page = self.map_page
        sido = self.sido_var.get().strip()
        if not sido or not self._panel_sel:
            return
        await jq_set(page, self._panel_sel, "sido_text_area", sido)
        loaded = await wait_for_opts(
            page, self._panel_sel, "sgg_text_area", min_count=2, timeout_sec=8)
        if loaded:
            opts = await get_select_options(page, self._panel_sel, "sgg_text_area")
            self.root.after(0, lambda o=opts: self._fill_sgg_lb(o))
        else:
            self.log(f"  ⚠ 시군구 로드 실패: {sido}")

    def _fill_sgg_lb(self, opts: list):
        # "OO 시군구 전체" 항목을 예전에는 필터링했었는데, 실제 다운로드 실행 시에는
        # 이 항목이 시군구 단위 반복이 아니라 시도 단위로 한 번에 조회되는 경로라
        # 세밀한 해상도(예: 100M)처럼 시군구 단위 조회 자체가 안 되는 자료도
        # "시군구 전체"로는 정상적으로 받아진다. 그래서 다시 노출한다.
        self.sgg_lb.delete(0, tk.END)
        for o in opts:
            self.sgg_lb.insert(tk.END, o)

    # ── 메인 다운로드 루프 ──────────────────────────────────────────────────

    async def _download_one(self, page, panel_sel, period_val):
        """데이터 항목 1건(지역·기간 조합 하나)에 대해 조회 → 다운로드 모달 →
        파일트리 팝업 → 전송시작까지 수행한다. 실패하면 예외를 던지며, 호출자
        (_async_run)가 MAX_ATTEMPTS 회까지 재시도한다.

        period_val이 None이면 기준년도가 데이터 체인에서 이미 지정된 카테고리
        (국토모니터링)라는 뜻이므로 기간 select 조작을 건너뛴다."""
        # ── 기간 선택 (시도/시군구는 지역 루프에서 이미 설정) ──────────────
        if period_val is not None:
            set_result = await page.evaluate(f"""() => {{
                let sel = null;
                for (const p of document.querySelectorAll('{panel_sel}')) {{
                    sel = p.querySelector('select[name="year_text_area"]');
                    if (sel) break;
                }}
                if (!sel) return 'no_sel';
                const opt = Array.from(sel.options)
                    .find(o => o.value === '{period_val}');
                if (!opt) return 'not_found';
                sel.value = opt.value;
                if (window.jQuery) jQuery(sel).trigger('change');
                else sel.dispatchEvent(new Event('change', {{bubbles:true}}));
                return 'ok';
            }}""")
            if set_result != 'ok':
                raise Exception(f"기간 선택 실패: {set_result}")

        # 조회 버튼 클릭
        await close_alert(page)
        await page.evaluate(f"""() => {{
            for (const p of document.querySelectorAll('{panel_sel}')) {{
                const btn = p.querySelector('button.searchBtn');
                if (btn) {{ btn.click(); break; }}
            }}
        }}""")

        # statsDownload 버튼 등장 대기 (최대 20초)
        stats_btn_ready = await wait_for_element_js(
            page,
            "document.querySelector('main') && "
            "(document.querySelector('main').className.includes('_resultOn') || "
            "!!document.querySelector('button.statsDownload'))",
            timeout_sec=20,
        )
        await close_alert(page)

        if not stats_btn_ready:
            diag = await page.evaluate("""() => ({
                mainCls: (document.querySelector('main') || {}).className || 'no_main',
                dlBtn: !!(document.querySelector('button.statsDownload')),
            })""")
            raise Exception(f"검색 결과 로드 타임아웃: {diag}")

        # ── 다운로드 단계 ────────────────────────────────────────────────────
        popup = None
        dlg_handler = None      # page.once("dialog", ...)로 등록한 핸들러
        try:
            # Step 0: 이전 모달/알림 정리
            await close_alert(page)

            # Step 1: statsDownload 클릭
            s1 = await page.evaluate("""() => {
                const btn = document.querySelector('button.statsDownload');
                if (btn) { btn.click(); return 'ok'; }
                return 'not_found';
            }""")
            if s1 != 'ok':
                raise Exception(f"statsDownload 버튼 없음: {s1}")

            # Step 2: 모달이 실제로 화면에 보일 때까지 대기
            # offsetParent !== null → 요소가 DOM에 존재하고 실제 visible 상태
            if not await wait_for_element_js(
                    page,
                    "(() => { const inp = document.querySelector('input#agree'); "
                    "return inp && inp.offsetParent !== null; })()",
                    timeout_sec=20):
                raise Exception("다운로드 모달 등장 타임아웃")

            # Step 2.5: select#onPurchsPurps 에 실제 옵션이 로드될 때까지 대기
            # '선택하세요' 같은 빈 value 가 아닌 실제 value 가 생기면 로딩 완료
            purpose_loaded = await wait_for_element_js(
                page,
                "(() => {"
                "  const s = document.querySelector('select#onPurchsPurps');"
                "  if (!s || s.offsetParent === null) return false;"
                "  return Array.from(s.options).some(o => o.value.trim() !== '');"
                "})()",
                timeout_sec=10)
            self.log(f"    사용목적 로딩: {'ok' if purpose_loaded else '타임아웃'}")

            # 이전 선택값이 완전히 로드된 후 0.5초 안정화 대기
            await asyncio.sleep(0.5)

            # Step 2.6: 동의합니다 클릭 — JS 직접 클릭으로 actionability 우회
            await page.evaluate("""() => {
                const inp = document.querySelector('input#agree');
                if (!inp) return;
                inp.checked = true;
                inp.click();
                inp.dispatchEvent(new Event('change', {bubbles: true}));
                if (window.jQuery) jQuery(inp).trigger('click').trigger('change');
            }""")
            self.log("    동의: ok")

            # Step 2.7: 사용목적 현재 값 확인 — 이미 선택돼 있으면 그대로,
            #           미선택이면 첫 번째 유효 옵션 fallback
            purpose_result = await page.evaluate("""() => {
                const sel = document.querySelector('select#onPurchsPurps');
                if (!sel) return 'no_select';
                const opts = Array.from(sel.options)
                    .filter(o => o.value.trim() !== '');
                if (opts.length === 0) return 'no_options';
                if (sel.value.trim()) {
                    return 'ok(기존):' +
                        sel.options[sel.selectedIndex].text.trim();
                }
                // 미선택 → 첫 번째 유효 옵션 선택
                sel.value = opts[0].value;
                if (window.jQuery) jQuery(sel).trigger('change');
                else sel.dispatchEvent(
                    new Event('change', {bubbles: true}));
                return 'ok(신규):' + opts[0].text.trim();
            }""")
            self.log(f"    사용목적: {purpose_result}")

            # Step 3: 다운로드 버튼 확인 (모달이 이미 열려 있으므로 즉시)
            dl_btn = page.locator('#modalPopup').locator('button', has_text='다운로드')
            if await dl_btn.count() == 0:
                raise Exception("다운로드 버튼 없음: #modalPopup")
            self.log("    다운로드 버튼: ok")

            # Step 4: 다운로드 버튼 클릭 → 파일트리 팝업 창 대기
            # 브라우저 기본 window.open 동작 그대로 사용 (개입 없음)
            async def _on_dialog(dialog):
                self.log(f"    다이얼로그: {dialog.message[:60]}")
                await dialog.accept()
            dlg_handler = _on_dialog
            page.once("dialog", _on_dialog)

            pages_before = {
                id(p)
                for ctx in self.browser.contexts
                for p in ctx.pages
            }

            await dl_btn.click()
            self.log("    다운로드 버튼 클릭 완료")

            # 새 창/페이지가 나타날 때까지 폴링 (최대 30초)
            for _ in range(60):
                if self.stop_event.is_set():
                    raise Exception("중지 요청으로 중단")
                for ctx in self.browser.contexts:
                    for p in ctx.pages:
                        if id(p) not in pages_before and not p.is_closed():
                            popup = p
                            break
                    if popup:
                        break
                if popup:
                    self.log("    팝업 감지: ok")
                    break
                await close_alert(page)
                await asyncio.sleep(0.5)

            if popup is None:
                raise Exception("파일트리 팝업 열기 실패")

            try:
                await popup.wait_for_load_state('load', timeout=60_000)
            except Exception:
                pass  # load 타임아웃 무시 (대용량 시도 단위 조회 시 오래 걸릴 수 있음)
            try:
                await popup.wait_for_load_state('networkidle', timeout=20_000)
            except Exception:
                pass  # networkidle 타임아웃 무시 (다운로드 중일 수 있음)

            # 실제 파일트리 항목이 DOM에 나타날 때까지 별도 대기
            # (load/networkidle 이벤트와 무관하게, 대용량 목록은 렌더링이 더 늦게 끝날 수 있음)
            await wait_for_element_js(
                popup,
                "document.querySelectorAll('div.irx-file-inner-wrapper').length > 0",
                timeout_sec=60)
            self.log("    파일트리 팝업 로드 완료")

            # Step 5: .zip 항목 체크
            zip_checked = await popup.evaluate("""() => {
                const wrappers = document.querySelectorAll('div.irx-file-inner-wrapper');
                let found = 0;
                for (const w of wrappers) {
                    if ((w.getAttribute('title') || '').endsWith('.zip')) {
                        const cb = w.querySelector('div.tree-icon.filetree-checkbox');
                        if (cb) { cb.click(); found++; }
                    }
                }
                return found > 0 ? 'zip:ok(' + found + ')' : 'zip_not_found';
            }""")
            self.log(f"    zip 체크: {zip_checked}")
            if 'not_found' in zip_checked:
                raise Exception(f"zip 항목 없음: {zip_checked}")

            # Step 6: 선택 다운로드 버튼 클릭
            sel_dl_btn = popup.locator(
                'button.left[onclick="control.downloadSelectedFiles();"]')
            if await sel_dl_btn.count() == 0:
                raise Exception("선택 다운로드 버튼 없음")
            await sel_dl_btn.click()
            self.log("    선택 다운로드 클릭 완료")

            # Step 7: 전송시작 버튼 등장 대기 → 클릭
            if not await wait_for_element_js(
                    popup,
                    "Array.from(document.querySelectorAll('button.irx_controller'))"
                    ".some(b => b.textContent.includes('전송시작'))",
                    timeout_sec=10):
                raise Exception("전송시작 버튼 등장 타임아웃")
            send_btn = popup.locator('button.irx_controller', has_text='전송시작')
            await send_btn.click()
            self.log("    전송시작 클릭 완료")

            # Step 8: 팝업 닫힘 대기 (최대 60초)
            for _ in range(120):
                if popup.is_closed():
                    break
                await asyncio.sleep(0.5)
            else:
                self.log("    ⚠ 팝업 닫힘 타임아웃, 강제 진행")
            self.log("    팝업 닫힘 확인")
        except Exception:
            # 재시도가 깨끗한 상태에서 시작하도록, 열려 있는 파일트리 팝업을 닫는다.
            # (닫지 않으면 다음 시도의 "새 창 감지"가 이 창을 새 팝업으로 오인한다)
            if popup is not None and not popup.is_closed():
                try:
                    await popup.close()
                except Exception:
                    pass
            raise
        finally:
            # once() 로 등록한 dialog 핸들러는 다이얼로그가 실제로 뜨지 않으면
            # 그대로 남는다. 항목마다(재시도까지 하면 항목당 최대 3번) 쌓이면
            # 나중에 다이얼로그가 하나 떴을 때 누적된 핸들러가 전부 호출되어
            # "이미 처리된 dialog"에 accept()를 다시 호출하는 오류가 난다.
            if dlg_handler is not None:
                try:
                    page.remove_listener("dialog", dlg_handler)
                except Exception:
                    pass

    async def _apply_data_chain(self, page, panel_sel, item, chain_timeout):
        """데이터 항목의 체인 값을 사이트 select에 순서대로 적용."""
        chain       = item["chain"]
        chain_vals  = item["chain_vals"]
        chain_vmaps = item["chain_vmaps"]

        # 첫 번째 select 로드 대기
        await wait_for_opts(page, panel_sel, chain[0][1],
                            min_count=1, timeout_sec=chain_timeout)

        self.log("⚙ 데이터 옵션 설정 중...")
        for i, (label, sname) in enumerate(chain):
            display = chain_vals.get(sname, "")
            if not display:
                self.log(f"  {label}: (건너뜀)")
                continue
            actual = chain_vmaps.get(sname, {}).get(display, display)
            self.log(f"  {label}: {display}")
            await jq_set(page, panel_sel, sname, actual)
            if i + 1 < len(chain):
                next_label = chain[i + 1][0]
                next_sname = chain[i + 1][1]
                self.log(f"  {next_label} 로딩 대기 중...")
                ok = await wait_for_opts(page, panel_sel, next_sname,
                                         min_count=1, timeout_sec=chain_timeout)
                if not ok:
                    self.log(f"  ⚠ {next_label} 로드 시간 초과, 계속 진행")

    async def _async_run(self, cfg: dict):
        page = self.map_page

        regions = []
        for r in cfg["regions"]:
            parts = r.split(" ", 1)
            if len(parts) == 2:
                regions.append((parts[0], parts[1]))

        items = cfg["items"]

        # 총 건수 = Σ(데이터 항목별 지역 수 × 기간 수).
        # 기간이 없는 카테고리(국토모니터링)는 항목당 1건으로 센다.
        total = sum(len(regions) * max(1, len(it["periods"])) for it in items)
        done = 0
        failed = 0        # 실패로 처리된 "건" 수 (errors 메시지 수와 다를 수 있음 —
                          #  데이터 항목 통째 스킵은 메시지 1줄에 여러 건이 걸린다)
        errors = []

        self.log(f"\n{'='*60}")
        self.log(f"▶ 시작: 데이터 {len(items)}개 × 지역 {len(regions)}개 = 총 {total}건")
        self.log(f"   (실패 시 항목당 최대 {MAX_ATTEMPTS}회까지 재시도 후 다음으로 진행)")
        self.log(f"{'='*60}")
        self.root.after(0, lambda t=total: (
            self.progress_var.set(0),
            self.lbl_progress.configure(text=f"0 / {t}"),
            self._set_stats(0, 0),
        ))

        def _bump(n=1):
            """진행 카운터 + 성공/실패 집계 갱신.
            (성공/실패/스킵 모두 여기서 1건으로 센다. 호출 전에 failed 를 먼저
            갱신해 두어야 집계가 어긋나지 않는다.)"""
            nonlocal done
            done += n
            pct = done / total * 100 if total else 0
            self.root.after(0, lambda p=pct, d=done, t=total,
                                   o=done - failed, g=failed: (
                self.progress_var.set(p),
                self.lbl_progress.configure(text=f"{d} / {t}"),
                self._set_stats(o, g),
            ))

        for idx_item, item in enumerate(items, 1):
            if self.stop_event.is_set():
                self.log("⏹ 중지됨")
                break

            cat     = item["category"]
            chain   = item["chain"]
            periods = list(item["periods"]) or [None]   # None = 기간 select 조작 없음
            unit    = len(regions) * len(periods)

            self.log(f"\n{'─'*60}")
            self.log(f"■ 데이터 {idx_item}/{len(items)}: {item['label']}")
            self.log(f"{'─'*60}")

            # ── 탭·패널 활성화 ────────────────────────────────────────────────
            # 목록에 서로 다른 분류가 섞여 있을 수 있으므로 항목마다 탭을 다시 맞춘다.
            # 실제로 탭을 눌러 전환한 경우에만 리렌더 시간을 조금 준다
            # (이미 켜져 있는 탭이면 클릭하지 않으므로 대기도 불필요).
            tab_res = await activate_category_tab(page, cat)
            if isinstance(tab_res, str) and tab_res.startswith("ok:"):
                await asyncio.sleep(0.4)
            panel_sel = await find_panel_sel(page, cat)
            if not panel_sel:
                self.log("❌ 패널 탐색 실패 → 이 데이터 항목 건너뜀")
                errors.append(f"{item['label']} : 패널 탐색 실패 ({unit}건)")
                failed += unit
                _bump(unit)
                continue
            self._panel_sel = panel_sel
            run_chain_timeout = 60 if cat == "국토지표" else 15

            if not chain:
                self.log("❌ 데이터 체인이 비어 있습니다 → 이 데이터 항목 건너뜀")
                errors.append(f"{item['label']} : 데이터 체인 없음 ({unit}건)")
                failed += unit
                _bump(unit)
                continue

            # ── 데이터 옵션 체인 순서대로 설정 ───────────────────────────────
            try:
                await self._apply_data_chain(page, panel_sel, item, run_chain_timeout)
            except Exception as e:
                self.log(f"❌ 데이터 옵션 설정 실패: {e} → 이 데이터 항목 건너뜀")
                errors.append(f"{item['label']} : 데이터 옵션 설정 실패 ({unit}건) — {e}")
                failed += unit
                _bump(unit)
                continue

            if periods[0] is None:
                self.log("  ℹ 기준년도는 데이터 설정에서 이미 지정됨 (기간 반복 없음)")
            else:
                self.log(f"  기간: {periods[0]} ~ {periods[-1]}  ({len(periods)}개)")

            # ── 지역 루프 ─────────────────────────────────────────────────────
            for sido, sigungu in regions:
                if self.stop_event.is_set():
                    break   # 바깥 데이터 항목 루프에서 한 번만 로그를 남긴다
                self.log(f"\n📍 {sido} {sigungu}")

                # ── 시도/시군구는 지역당 한 번만 설정 (여기도 최대 3회 재시도) ──
                region_err = None
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    if self.stop_event.is_set():
                        region_err = Exception("중지 요청으로 중단")
                        break
                    try:
                        if attempt > 1:
                            self.log(f"    ↻ 지역 설정 재시도 {attempt}/{MAX_ATTEMPTS}")
                        await jq_set(page, panel_sel, "sido_text_area", sido)
                        if not await wait_for_opts(page, panel_sel, "sgg_text_area",
                                                   min_count=2, timeout_sec=10):
                            raise Exception("sgg 로딩 타임아웃")
                        await jq_set(page, panel_sel, "sgg_text_area", sigungu)
                        if not await wait_for_opts(page, panel_sel, "year_text_area",
                                                   min_count=1, timeout_sec=10):
                            self.log("  ⚠ 기간 목록 로딩 시간 초과, 계속 진행")
                        region_err = None
                        break
                    except Exception as e:
                        region_err = e
                        self.log(f"  ⚠ 시도/시군구 설정 시도 {attempt}/{MAX_ATTEMPTS} 실패: {e}")
                        await close_alert(page)
                        if attempt < MAX_ATTEMPTS and not self.stop_event.is_set():
                            await asyncio.sleep(RETRY_DELAY_SEC * attempt)

                if region_err is not None:
                    self.log(f"  ❌ 시도/시군구 설정 실패: {region_err} → 지역 스킵 "
                             f"({len(periods)}건)")
                    errors.extend(
                        f"{item['label']} / {sido} {sigungu} {p or '-'}: "
                        f"지역 설정 실패 ({region_err})"
                        for p in periods)
                    failed += len(periods)
                    _bump(len(periods))
                    continue

                for period_val in periods:
                    if self.stop_event.is_set():
                        break

                    tag = f"[{done + 1}/{total}] {period_val or '(기간없음)'}"
                    self.log(f"  ▷ {tag}")

                    # ── 실패 시 최대 MAX_ATTEMPTS회까지 재시도 ────────────────
                    last_err = None
                    for attempt in range(1, MAX_ATTEMPTS + 1):
                        if self.stop_event.is_set():
                            last_err = Exception("중지 요청으로 중단")
                            break
                        try:
                            if attempt > 1:
                                self.log(f"    ↻ 재시도 {attempt}/{MAX_ATTEMPTS}")
                            await self._download_one(page, panel_sel, period_val)
                            last_err = None
                            break
                        except Exception as e:
                            last_err = e
                            self.log(f"    ⚠ 시도 {attempt}/{MAX_ATTEMPTS} 실패: {e}")
                            await close_alert(page)
                            if attempt < MAX_ATTEMPTS and not self.stop_event.is_set():
                                # 사이트가 직전 요청을 정리할 시간을 주고 재시도
                                await asyncio.sleep(RETRY_DELAY_SEC * attempt)

                    if last_err is not None:
                        failed += 1      # _bump 가 읽으므로 반드시 먼저 갱신
                    _bump()
                    if last_err is None:
                        self.log(f"  ✅ {tag}")
                    else:
                        if self.stop_event.is_set():
                            # 재시도 도중 중지를 누른 경우 — "3회 실패"가 아니다
                            self.log(f"  ⏹ {tag}: 중지 요청으로 중단")
                            errors.append(
                                f"{item['label']} / {sido} {sigungu} "
                                f"{period_val or '-'}: 중지 요청으로 중단")
                        else:
                            self.log(f"  ❌ {tag}: {MAX_ATTEMPTS}회 시도 후 실패 — {last_err}")
                            errors.append(
                                f"{item['label']} / {sido} {sigungu} "
                                f"{period_val or '-'}: {last_err}")

        self.log(f"\n{'='*60}")
        self.log(f"🎉 완료!  처리 {done}/{total}건 — 성공 {done - failed}건, 실패 {failed}건")
        if errors:
            self.log("\n오류 목록:")
            for e in errors:
                self.log(f"  - {e}")

        self.root.after(0, lambda: self._set_run_state(False))


# ── 진입점 ───────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app = NgiiDownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
