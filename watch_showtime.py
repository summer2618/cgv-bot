"""
CGV 특정 영화관 + 특정 상영관(IMAX 등) 신규 상영일자 오픈 감지 -> Discord 알림 봇

main.py(영화 단위 예매 오픈 감지)와는 다른 문제를 다룹니다:
CGV는 상영 스케줄을 몇 주 단위로 순차적으로 오픈합니다. 이미 전체 예매가 열려 있는
인기작(예: IMAX 상영관)이라도, "새로운 날짜의 회차"가 추가로 열리는 순간 잔여좌석이
확보될 확률이 높고, 인기 회차는 열리자마자 매진되기도 합니다. 이 스크립트는 그 "신규
날짜/회차 오픈" 순간을 감지해서 Discord로 즉시 알립니다.

동작 방식:
  1. Playwright로 CGV 영화 상세 페이지를 한 번 실제로 열어(cookie/Cloudflare 통과) 세션을 확보한다.
  2. 이후에는 그 세션(쿠키)을 재사용해 가벼운 API 요청만 주기적으로 보낸다.
     GET /api/v1/booking/searchSiteScnscYmdListByMov?coCd=A420&siteNo={극장}&movNo={영화}
         [&div=CUST_EXPO_MOVTYP_CD&attrCd={상영관타입}]
  3. 응답으로 오는 상영 가능 날짜 목록을, 직전에 저장해둔 목록과 비교한다.
  4. 새로 추가된 날짜가 있으면 Discord 웹훅으로 알림을 보낸다 (실제 예매/결제는 사람이 직접).

이 스크립트는 "예매 오픈 감지 + 알림"까지만 수행합니다. 좌석 선택, 로그인, 결제 등
실제 구매 과정은 자동화하지 않습니다 (CGV 로그인 비밀번호나 결제 정보를 대신 입력하는
기능은 포함하지 않습니다 - 알림을 받은 사람이 직접 빠르게 예매해야 합니다).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cgv-showtime-watch")

CO_CD = "A420"
BASE = "https://cgv.co.kr"
SHOWTIME_DATES_PATH = "/api/v1/booking/searchSiteScnscYmdListByMov"
SCHEDULE_PATH = "/api/v1/booking/searchSchByMov"
REGN_SEARCH_PATH = "/api/v1/booking/searchRegnList"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = Path(os.environ.get("SHOWTIME_STATE_FILE", "showtime_state.json"))
POLL_INTERVAL_SECONDS = int(os.environ.get("SHOWTIME_POLL_INTERVAL_SECONDS", "15"))
COOKIE_REWARM_EVERY = int(os.environ.get("COOKIE_REWARM_EVERY_CYCLES", "100"))
MIN_SEAT_INCREASE = int(os.environ.get("SHOWTIME_MIN_SEAT_INCREASE", "2"))
# 이 날짜(YYYYMMDD) 이후 회차만 좌석 상세까지 체크. 비워두면 열려있는 모든 날짜를 체크.
SEAT_CHECK_FROM_DATE = os.environ.get("SHOWTIME_SEAT_CHECK_FROM_DATE", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# attrCd -> 상영관 이름(scnsNm)에 포함될 것으로 기대되는 키워드.
# searchSchByMov는 극장의 모든 상영관(2D/IMAX/DOLBY 등)을 한꺼번에 반환하므로,
# 이 키워드로 원하는 포맷의 회차만 걸러낸다.
ATTR_KEYWORDS = {
    "04": "IMAX",
    "08": "SCREENX",
    "03": "4DX",
    "06": "DOLBY",
    "09": "4DX",  # ULTRA 4DX
}


@dataclass
class Watch:
    label: str
    mov_no: str
    site_no: str
    attr_cd: str | None  # None = 전체 상영관 (포맷 필터 없음)

    @property
    def key(self) -> str:
        return f"{self.mov_no}:{self.site_no}:{self.attr_cd or 'ALL'}"

    @property
    def detail_url(self) -> str:
        return f"{BASE}/cnm/cgvChart/movieChart/{self.mov_no}"

    @property
    def api_url(self) -> str:
        url = (
            f"{BASE}{SHOWTIME_DATES_PATH}?coCd={CO_CD}"
            f"&siteNo={self.site_no}&movNo={self.mov_no}"
        )
        if self.attr_cd:
            url += f"&div=CUST_EXPO_MOVTYP_CD&attrCd={self.attr_cd}"
        return url

    def schedule_url(self, scn_ymd: str) -> str:
        return (
            f"{BASE}{SCHEDULE_PATH}?coCd={CO_CD}&siteNo={self.site_no}"
            f"&scnYmd={scn_ymd}&movNo={self.mov_no}&rtctlScopCd=08"
        )


def load_watches() -> list[Watch]:
    """
    SHOWTIME_WATCHES 형식: "라벨|movNo|siteNo|attrCd" 를 세미콜론(;)으로 구분.
    attrCd는 생략 가능(전체 상영관 대상): "라벨|movNo|siteNo"

    예:
      SHOWTIME_WATCHES=용산아이파크몰 IMAX 오디세이|30001323|0013|04
    """
    raw = os.environ.get("SHOWTIME_WATCHES", "").strip()
    if not raw:
        log.error("SHOWTIME_WATCHES 환경변수가 비어 있습니다.")
        sys.exit(1)

    watches = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) < 3:
            log.error("SHOWTIME_WATCHES 항목 형식 오류: %r (라벨|movNo|siteNo[|attrCd])", chunk)
            sys.exit(1)
        label, mov_no, site_no = parts[0], parts[1], parts[2]
        attr_cd = parts[3] if len(parts) > 3 and parts[3] else None
        watches.append(Watch(label=label, mov_no=mov_no, site_no=site_no, attr_cd=attr_cd))
    return watches


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state 파일 파싱 실패, 초기화합니다.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fmt_date(ymd: str) -> str:
    try:
        d = datetime.strptime(ymd, "%Y%m%d")
        return f"{d.month}/{d.day}({WEEKDAY_KR[d.weekday()]})"
    except ValueError:
        return ymd


def send_discord_text(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")
        return
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    if resp.status_code >= 300:
        log.error("Discord 메시지 전송 실패 (%s): %s", resp.status_code, resp.text)


def send_new_dates_alert(watch: Watch, new_dates: list[str]) -> None:
    dates_str = ", ".join(fmt_date(d) for d in sorted(new_dates))
    message = (
        f"🎬 **[{watch.label}] 신규 상영 회차가 열렸어요!**\n"
        f"새로 열린 날짜: {dates_str}\n"
        f"지금 바로 예매하러 가기: {watch.detail_url}"
    )
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    if resp.status_code >= 300:
        log.error("Discord 알림 전송 실패 (%s): %s", resp.status_code, resp.text)
    else:
        log.info("[%s] 신규 날짜 알림 전송: %s", watch.label, dates_str)


def fmt_time(hhmm: str) -> str:
    """CGV의 scnsrtTm(예: '2455')을 'HH:MM' 형태로. 24시 넘어가면 '익일 HH:MM'."""
    try:
        v = int(hhmm)
        hour, minute = v // 100, v % 100
        prefix = ""
        if hour >= 24:
            hour -= 24
            prefix = "익일 "
        return f"{prefix}{hour:02d}:{minute:02d}"
    except (ValueError, TypeError):
        return hhmm


def showtime_key(item: dict) -> str:
    return f"{item.get('scnYmd')}_{item.get('scnsNo')}_{item.get('scnsrtTm')}"


def send_seat_increase_alert(watch: Watch, item: dict, prev_cnt: int, curr_cnt: int) -> None:
    when = f"{fmt_date(item.get('scnYmd', ''))} {fmt_time(item.get('scnsrtTm', ''))}"
    screen = item.get("expoScnsNm") or item.get("scnsNm") or ""
    message = (
        f"🚨 **[{watch.label}] 잔여좌석이 늘었어요!**\n"
        f"{when} · {screen} — {prev_cnt}석 → {curr_cnt}석 (+{curr_cnt - prev_cnt})\n"
        f"지금 바로 예매하러 가기: {watch.detail_url}"
    )
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    if resp.status_code >= 300:
        log.error("Discord 알림 전송 실패 (%s): %s", resp.status_code, resp.text)
    else:
        log.info("[%s] 잔여좌석 증가 알림 전송: %s (%d->%d)", watch.label, when, prev_cnt, curr_cnt)


def warm_up(context, watch_for_referer: Watch) -> None:
    """실제 페이지를 한 번 로드해서 Cloudflare 통과 쿠키를 (재)확보한다."""
    page = context.new_page()
    try:
        page.goto(watch_for_referer.detail_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1000)
        log.info("세션 워밍업 완료 (Referer: %s)", watch_for_referer.detail_url)
    finally:
        page.close()


def fetch_dates(context, watch: Watch) -> set[str] | None:
    try:
        resp = context.request.get(
            watch.api_url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": watch.detail_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15000,
        )
    except Exception:
        log.exception("[%s] API 요청 중 오류", watch.label)
        return None

    if resp.status != 200:
        log.warning("[%s] API 상태 코드 %s (세션 만료 가능성, 다음 사이클에 재워밍업)", watch.label, resp.status)
        return None

    try:
        body = resp.json()
    except Exception:
        log.warning("[%s] JSON 파싱 실패 (Cloudflare 차단 페이지일 수 있음)", watch.label)
        return None

    if body.get("statusCode") != 0:
        log.warning("[%s] API statusMessage: %s", watch.label, body.get("statusMessage"))
        return None

    return {item["scnYmd"] for item in body.get("data", [])}


def fetch_showtimes(context, watch: Watch, scn_ymd: str) -> list[dict] | None:
    """특정 날짜의 회차 목록(잔여좌석 포함)을 가져와서, watch.attr_cd에 해당하는
    포맷(예: IMAX)의 회차만 걸러 반환한다."""
    try:
        resp = context.request.get(
            watch.schedule_url(scn_ymd),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": watch.detail_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15000,
        )
    except Exception:
        log.exception("[%s] %s 회차 조회 중 오류", watch.label, scn_ymd)
        return None

    if resp.status != 200:
        log.warning("[%s] %s 회차 조회 상태 코드 %s", watch.label, scn_ymd, resp.status)
        return None

    try:
        body = resp.json()
    except Exception:
        log.warning("[%s] %s 회차 조회 JSON 파싱 실패", watch.label, scn_ymd)
        return None

    if body.get("statusCode") != 0:
        log.warning("[%s] %s 회차 조회 statusMessage: %s", watch.label, scn_ymd, body.get("statusMessage"))
        return None

    items = body.get("data", []) or []
    keyword = ATTR_KEYWORDS.get(watch.attr_cd) if watch.attr_cd else None
    if keyword:
        items = [
            it for it in items
            if keyword in (it.get("expoScnsNm") or "") or keyword in (it.get("scnsNm") or "")
        ]
    return items


def check_once(context, watches: list[Watch], state: dict) -> bool:
    """하나라도 세션 재워밍업이 필요했으면 True 반환."""
    needs_rewarm = False

    for watch in watches:
        current_dates = fetch_dates(context, watch)
        if current_dates is None:
            needs_rewarm = True
            continue

        prev_entry = state.get(watch.key)
        seen_before = prev_entry is not None
        if isinstance(prev_entry, dict):
            prev_dates = set(prev_entry.get("dates", []))
            prev_seats = prev_entry.get("seats", {})
        elif isinstance(prev_entry, list):  # 이전 버전 state 포맷 호환
            prev_dates = set(prev_entry)
            prev_seats = {}
        else:
            prev_dates = set()
            prev_seats = {}

        log.info(
            "[%s] 현재 오픈된 날짜 %d개: %s",
            watch.label, len(current_dates), ", ".join(sorted(current_dates)),
        )

        if seen_before:
            new_dates = current_dates - prev_dates
            if new_dates:
                send_new_dates_alert(watch, sorted(new_dates))

        # SEAT_CHECK_FROM_DATE 이후 날짜만 회차별 잔여좌석까지 조회 (비어있으면 전체)
        curr_seats: dict[str, int] = {}
        dates_to_check = sorted(
            d for d in current_dates
            if not SEAT_CHECK_FROM_DATE or d >= SEAT_CHECK_FROM_DATE
        )
        for scn_ymd in dates_to_check:
            items = fetch_showtimes(context, watch, scn_ymd)
            if items is None:
                needs_rewarm = True
                continue
            for item in items:
                try:
                    cnt = int(item.get("frSeatCnt", 0))
                except (ValueError, TypeError):
                    continue
                key = showtime_key(item)
                curr_seats[key] = cnt

                if seen_before and key in prev_seats:
                    diff = cnt - prev_seats[key]
                    if diff >= MIN_SEAT_INCREASE:
                        send_seat_increase_alert(watch, item, prev_seats[key], cnt)

        state[watch.key] = {"dates": sorted(current_dates), "seats": curr_seats}
        save_state(state)

    return needs_rewarm


def find_site(mov_no: str, keyword: str) -> None:
    """SHOWTIME_WATCHES에 쓸 siteNo를 찾기 위한 헬퍼. 브라우저로 검색 API를 호출한다."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, locale="ko-KR")
        detail_url = f"{BASE}/cnm/cgvChart/movieChart/{mov_no}"
        page = context.new_page()
        page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1000)

        resp = context.request.get(
            f"{BASE}{REGN_SEARCH_PATH}?movNo={mov_no}&coCd={CO_CD}&srchKwrd={keyword}",
            headers={"Accept": "application/json, text/plain, */*", "Referer": detail_url},
        )
        body = resp.json()
        found = False
        for regn in body.get("data", []):
            for site in regn.get("siteList", []):
                found = True
                print(f"siteNo={site['siteNo']}  siteNm={site['siteNm']}")
        if not found:
            print("검색 결과가 없습니다. movNo가 맞는지, 그 영화관에서 실제로 상영 중인지 확인하세요.")
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Discord 웹훅 테스트 메시지 전송")
    parser.add_argument(
        "--find-site",
        nargs=2,
        metavar=("MOVNO", "KEYWORD"),
        help="예: --find-site 30001323 용산아이파크몰",
    )
    args = parser.parse_args()

    if args.find_site:
        find_site(args.find_site[0], args.find_site[1])
        return

    if not DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL 환경변수가 설정되지 않았습니다. .env를 확인하세요.")
        sys.exit(1)

    if args.test:
        send_discord_text("✅ CGV 신규 회차 알림봇 테스트 메시지입니다. 웹훅 연결이 정상입니다.")
        return

    watches = load_watches()
    state = load_state()
    log.info("모니터링 대상: %s", ", ".join(w.label for w in watches))
    log.info("폴링 주기: %d초", POLL_INTERVAL_SECONDS)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, locale="ko-KR")

        warm_up(context, watches[0])
        cycle = 0
        try:
            while True:
                cycle += 1
                try:
                    needs_rewarm = check_once(context, watches, state)
                except Exception:
                    log.exception("체크 사이클 중 예상치 못한 오류 발생")
                    needs_rewarm = True

                if needs_rewarm or cycle % COOKIE_REWARM_EVERY == 0:
                    warm_up(context, watches[0])

                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("종료합니다.")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
