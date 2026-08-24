"""
CGV 영화 예매 오픈 감지 -> Discord 알림 봇

동작 방식:
  1. Playwright(헤드리스 크로미움)로 CGV 영화 상세 페이지를 실제 브라우저처럼 연다.
  2. 페이지 로드 시 CGV 프론트엔드가 자체적으로 호출하는
     `/api/v1/content/cgvct/searchMovDtlMain` 응답을 가로채 예매 가능 여부(atktPsblYn)를 읽는다.
  3. 직전 상태와 비교해 N(예매 예정) -> Y(예매 가능)로 바뀐 순간에만 Discord 웹훅으로 알림을 보낸다.
  4. 이 과정을 POLL_INTERVAL_SECONDS 간격으로 반복한다.

설정은 .env 파일(또는 환경변수)로 한다. .env.example 참고.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cgv-ticket-alert")

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
NOTIFY_IF_ALREADY_OPEN = os.environ.get("NOTIFY_IF_ALREADY_OPEN", "false").lower() == "true"
CO_CD = "A420"  # CGV 회사 코드 (고정값)
DETAIL_API_PATH = "/api/v1/content/cgvct/searchMovDtlMain"

if not DISCORD_WEBHOOK_URL:
    log.error("DISCORD_WEBHOOK_URL 환경변수가 설정되지 않았습니다. .env를 확인하세요.")
    sys.exit(1)


@dataclass
class Movie:
    mov_no: str
    label: str  # 알림에 표시할 이름 (사람이 지정, API에서 온 실제 영화명과 다를 수 있음)


def load_movies() -> list[Movie]:
    """
    MOVIE_IDS 환경변수 형식: "30001350:경주기행,30001234:다른영화"
    영화명은 생략 가능: "30001350,30001234"
    movNo는 CGV 영화 상세 페이지 URL에서 확인:
      https://cgv.co.kr/cnm/cgvChart/movieChart/{movNo}
    """
    raw = os.environ.get("MOVIE_IDS", "").strip()
    if not raw:
        log.error("MOVIE_IDS 환경변수가 비어 있습니다. 예: 30001350:경주기행")
        sys.exit(1)

    movies = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            mov_no, label = chunk.split(":", 1)
        else:
            mov_no, label = chunk, chunk
        movies.append(Movie(mov_no=mov_no.strip(), label=label.strip()))
    return movies


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json 파싱 실패, 초기화합니다.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_discord_notification(movie: Movie, data: dict) -> None:
    mov_nm = data.get("movNm") or movie.label
    rls_ymd = data.get("rlsYmd") or ""
    rls_fmt = f"{rls_ymd[0:4]}-{rls_ymd[4:6]}-{rls_ymd[6:8]}" if len(rls_ymd) == 8 else rls_ymd
    detail_url = f"https://cgv.co.kr/cnm/cgvChart/movieChart/{movie.mov_no}"

    payload = {
        "username": "CGV 예매 알림봇",
        "embeds": [
            {
                "title": f"🎬 [{mov_nm}] 예매가 열렸어요!",
                "url": detail_url,
                "description": f"개봉일: {rls_fmt}\n[바로 예매하러 가기]({detail_url})",
                "color": 0xE31937,
            }
        ],
    }
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    if resp.status_code >= 300:
        log.error("Discord 알림 전송 실패 (%s): %s", resp.status_code, resp.text)
    else:
        log.info("Discord 알림 전송 완료: %s", mov_nm)


def send_discord_text(message: str) -> None:
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    if resp.status_code >= 300:
        log.error("Discord 메시지 전송 실패 (%s): %s", resp.status_code, resp.text)


def fetch_booking_status(page, movie: Movie) -> dict | None:
    """movie 상세 페이지를 열고 searchMovDtlMain 응답 JSON을 반환한다. 실패 시 None."""
    detail_url = f"https://cgv.co.kr/cnm/cgvChart/movieChart/{movie.mov_no}"
    try:
        with page.expect_response(
            lambda r: DETAIL_API_PATH in r.url, timeout=20000
        ) as response_info:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
        response = response_info.value
        if response.status != 200:
            log.warning("[%s] API 응답 상태 코드 %s", movie.label, response.status)
            return None
        body = response.json()
        if body.get("statusCode") != 0:
            log.warning("[%s] API statusMessage: %s", movie.label, body.get("statusMessage"))
            return None
        return body.get("data")
    except PWTimeoutError:
        log.warning("[%s] 페이지/응답 대기 타임아웃", movie.label)
        return None
    except Exception:
        log.exception("[%s] 예매 상태 조회 중 오류", movie.label)
        return None


def check_once(page, movies: list[Movie], state: dict) -> None:
    for movie in movies:
        data = fetch_booking_status(page, movie)
        if data is None:
            continue

        is_open = data.get("atktPsblYn") == "Y"
        prev = state.get(movie.mov_no)
        prev_open = prev.get("atktPsblYn") == "Y" if prev else False

        mov_nm = data.get("movNm") or movie.label
        log.info(
            "[%s] 예매가능=%s 예매율=%s%%",
            mov_nm,
            data.get("atktPsblYn"),
            data.get("atktRate"),
        )

        should_notify = is_open and not prev_open and (prev is not None or NOTIFY_IF_ALREADY_OPEN)
        if should_notify:
            send_discord_notification(movie, data)

        state[movie.mov_no] = {
            "atktPsblYn": data.get("atktPsblYn"),
            "movNm": mov_nm,
            "checkedAt": int(time.time()),
        }
        save_state(state)

        time.sleep(random.uniform(1.5, 3.5))  # 같은 사이클 내 요청 간 간격 (매너 딜레이)


def main() -> None:
    movies = load_movies()
    state = load_state()

    log.info("모니터링 대상: %s", ", ".join(f"{m.label}({m.mov_no})" for m in movies))
    log.info("폴링 주기: %d초", POLL_INTERVAL_SECONDS)

    if "--test" in sys.argv:
        send_discord_text("✅ CGV 예매 알림봇 테스트 메시지입니다. 웹훅 연결이 정상입니다.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
        )
        page = context.new_page()

        try:
            while True:
                try:
                    check_once(page, movies, state)
                except Exception:
                    log.exception("체크 사이클 중 예상치 못한 오류 발생")
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("종료합니다.")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
