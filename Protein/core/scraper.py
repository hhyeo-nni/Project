"""쿠팡 수집기 (SeleniumBase UC 모드).

## 왜 이렇게 만들었나 (실측 근거, 2026-09-03)

쿠팡은 Akamai Bot Manager로 보호된다. 실제로 검증한 결과:

=================================================  ==========================================
방식                                                결과
=================================================  ==========================================
``requests`` + 완전한 브라우저 헤더                  ❌ 403 (모바일 도메인 루트까지 전부)
모바일 사이트 / 내부 JSON API 추정 경로              ❌ 403
Selenium 헤드리스                                   ❌ 403
Selenium 일반 창 (Chrome / Edge)                    ❌ ``_abck`` 가 봇 판정
undetected-chromedriver 3.5.5                       ❌ Chrome 151에서 더는 못 피함
nodriver 0.50.3                                     ❌ 챌린지까지만 도달
자동화 플래그 없는 순정 브라우저 + 나중에 CDP 접속   △ 홈·검색은 통과, 상품에서 차단
**SeleniumBase UC 모드**                            ✅ **통과**
=================================================  ==========================================

### 왜 SeleniumBase UC 모드만 되는가

``uc_open_with_reconnect(url, reconnect_time)`` 은 **페이지를 여는 동안 자동화 연결(CDP)을
아예 끊었다가**, 로딩이 끝난 뒤 다시 붙는다. Akamai의 행동분석 센서가 돌아가는 바로 그 순간에
자동화 흔적이 존재하지 않기 때문에 봇으로 판정되지 않는다.

일반 Selenium은 CDP가 계속 붙어 있어서 센서에 잡히고, 그 결과 ``_abck`` 쿠키가
``...~-1~...`` (봇 판정) 상태로 굳는다. 이 상태가 되면 홈페이지는 열려도
``/np/search`` 나 ``/vp/products`` 같은 보호 경로는 403이 난다.

### 반드시 지켜야 할 것

* **워밍업 경로**: 상품 URL로 바로 들어가면 실패한다. 반드시 ``홈 → 검색 → 상품`` 순서로
  자연스럽게 이동해야 통과한다. (UC 모드에서도 직접 진입은 실패하는 것을 확인)
* **헤드리스 금지**: 무조건 차단된다. 창을 숨기고 싶으면 화면 밖으로 밀어낸다.
* 한 사이클에 브라우저를 한 번만 띄우고 상품들을 순회한다.

가격 파싱은 클래스명이 아니라 **화면에 보이는 스타일**(폰트 크기·취소선)로 판별한다.
자세한 근거는 :mod:`core.parser` 참고.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.models import PricePoint, Product
from core.parser import build_price_point, clean_title, extract_ids, normalize_url
from core.paths import SELECTORS_FILE
from core.winhide import TaskbarHider

logger = logging.getLogger(__name__)

# 브라우저는 무겁고, 두 개가 동시에 뜨면 서로 방해한다.
# (예: 예약 점검 중에 설정 창에서 '상품 추가'를 누른 경우)
_BROWSER_LOCK = threading.Lock()
_LOCK_TIMEOUT_SEC = 300.0

HOME_URL = "https://www.coupang.com/"
SEARCH_URL = "https://www.coupang.com/np/search?q=%ED%94%84%EB%A1%9C%ED%8B%B4"

# 창을 화면 밖에 두는 좌표. 윈도우가 최소화 창을 보관하는 위치라 절대 보이지 않는다.
OFFSCREEN_POSITION = "-32000,-32000"

# uc_open_with_reconnect 의 연결 차단 시간(초). 길수록 안전하지만 느리다.
RECONNECT_HOME = 6
RECONNECT_PAGE = 6


# "이미 차단당한 상태"를 뜻하는 페이지 상태들. 더 두드려도 소용없다.
BLOCKED_STATES = ("access_denied", "coupang_403")


class ScraperBusyError(RuntimeError):
    """다른 작업이 브라우저를 쓰고 있어 시작하지 못했다."""


class ScraperBlockedError(RuntimeError):
    """쿠팡이 이 세션을 막고 있다. 이번 사이클은 더 시도해도 의미가 없다."""


DEFAULT_SELECTORS: dict[str, Any] = {
    "price_container": ".price-container",
    "og_title": "meta[property='og:title']",
    "og_image": "meta[property='og:image']",
    # selectors.json이 없거나 깨졌을 때만 쓰이는 폴백. **selectors.json과 반드시 동기화할 것.**
    # (품절 상품은 .price-container 자체가 렌더링되지 않으므로 이 목록이 품절 판정의 유일한 근거다)
    "sold_out": [
        "[class*='prod-not-find']",
        "[class*='buybox']",
        ".oos-label",
        "[class*='oos-']",
        "[class*='sold-out']",
        "[class*='soldout']",
    ],
    "sold_out_texts": [
        "일시품절", "품절", "판매중지", "현재 판매중인 상품이 아닙니다", "판매가 종료",
    ],
    "challenge_marker": "sec-if-cpt-container",
    "error_marker": "error403",
    "min_page_length": 30000,
}

# .price-container 안의 텍스트 조각을 스타일과 함께 뽑아온다.
# 자식 요소의 텍스트를 중복 수집하지 않도록, 각 요소의 "직속 텍스트 노드"만 읽는다.
_JS_EXTRACT_TOKENS = """
var sel = arguments[0];
var root = document.querySelector(sel);
if (!root) { return null; }
var out = [];
var all = [root].concat(Array.prototype.slice.call(root.querySelectorAll('*')));
for (var i = 0; i < all.length; i++) {
  var el = all[i];
  var own = '';
  for (var j = 0; j < el.childNodes.length; j++) {
    var n = el.childNodes[j];
    if (n.nodeType === 3) { own += n.textContent; }
  }
  own = own.replace(/\\s+/g, ' ').trim();
  if (!own) { continue; }
  var cs = window.getComputedStyle(el);
  var deco = (cs.textDecorationLine || cs.textDecoration || '');
  out.push({
    t: own,
    fs: parseFloat(cs.fontSize) || 0,
    lt: deco.indexOf('line-through') !== -1,
    fw: cs.fontWeight
  });
}
return out;
"""


def _load_selectors() -> dict[str, Any]:
    sels = dict(DEFAULT_SELECTORS)
    try:
        if SELECTORS_FILE.exists():
            with open(SELECTORS_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            sels.update({k: v for k, v in loaded.items() if not k.startswith("_")})
    except Exception:
        logger.warning("selectors.json 읽기 실패 — 기본값 사용", exc_info=True)
    return sels


class CoupangScraper:
    """쿠팡 상품 페이지에서 가격을 읽어온다.

    스케줄러와의 계약:
      * ``start()`` 는 브라우저를 띄우고 세션을 데운다. 한 사이클에 한 번만 부른다.
      * ``fetch(url)`` 은 **절대 예외를 던지지 않는다.** 실패하면 ``price=None`` 인
        PricePoint를 돌려준다.
      * ``stop()`` 은 몇 번을 불러도 안전하다.
    """

    def __init__(self, headless: bool = False, profile_dir: Optional[Path] = None) -> None:
        # headless=True 여도 진짜 헤드리스는 쓰지 않는다(쿠팡이 무조건 차단).
        # 대신 창을 화면 밖으로 보내 눈에 띄지 않게 한다.
        self.hide_window = bool(headless)
        # profile_dir 은 하위 호환을 위해 받기만 하고 쓰지 않는다.
        # UC 모드는 매번 깨끗한 임시 프로필을 쓰는 편이 안정적이다
        # (한 번 봇으로 찍힌 프로필은 _abck 가 오염된 채 굳어버리기 때문).
        self.profile_dir = profile_dir
        self.selectors = _load_selectors()
        self.driver = None
        self._warmed = False
        self._last_state = ""
        self._holds_lock = False
        self._hider: Optional[TaskbarHider] = None
        # 이번 사이클에서 차단이 확인됐는가 (스케줄러가 읽어 사이클을 조기 종료한다)
        self._blocked = False

    # ── 수명주기 ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.driver is not None:
            return
        from seleniumbase import Driver

        if not _BROWSER_LOCK.acquire(timeout=_LOCK_TIMEOUT_SEC):
            raise ScraperBusyError("다른 작업이 브라우저를 쓰고 있습니다. 잠시 후 다시 시도하세요.")
        self._holds_lock = True

        logger.info("브라우저 기동 (SeleniumBase UC 모드, 창숨김=%s)", self.hide_window)
        try:
            # 창 위치를 **기동 인자로** 넘긴다. 띄운 뒤에 옮기면 잠깐 화면에 번쩍이면서
            # 작업 중인 창 위로 튀어나오기 때문에, 처음부터 화면 밖에서 뜨게 해야 한다.
            # (-32000,-32000 은 윈도우가 최소화 창을 두는 좌표라 확실히 안 보인다.
            #  진짜 헤드리스는 쿠팡이 차단하므로 쓸 수 없다 — 화면 밖 배치는 통과 확인됨)
            kwargs: dict[str, Any] = dict(uc=True, headless=False, locale_code="ko")
            if self.hide_window:
                kwargs["window_position"] = OFFSCREEN_POSITION
                # 창이 생기는 즉시 작업표시줄에서 감추도록, 브라우저를 띄우기 **전에**
                # 감시를 시작한다. 창은 기동 후 약 4초 뒤에 생기고, 감시 스레드가
                # 50ms 안에 잡아내므로 작업표시줄 버튼은 사실상 보이지 않는다.
                self._hider = TaskbarHider()
                self._hider.start()

            self.driver = Driver(**kwargs)
            self.driver.set_page_load_timeout(90)
            if self.hide_window:
                # 기동 인자가 무시되는 경우를 대비한 이중 안전장치.
                try:
                    pos = self.driver.get_window_position()
                    if pos.get("x", 0) > -10000:
                        self.driver.set_window_position(-32000, -32000)
                except Exception:
                    logger.debug("창 숨기기 보정 실패", exc_info=True)
            self._warm_up()
        except Exception:
            self._stop_hider()
            self._release_lock()
            self.driver = None
            raise

    @property
    def is_blocked(self) -> bool:
        """쿠팡이 이 세션을 막고 있다고 판단됐는가.

        스케줄러는 이 값이 True가 되면 남은 상품을 건너뛰고 사이클을 끝내야 한다.
        차단된 상태에서 계속 두드리면 차단이 더 굳어지기 때문이다.
        """
        return self._blocked

    def _stop_hider(self) -> None:
        """작업표시줄 감시 스레드를 정리한다. 실패해도 무시한다."""
        h, self._hider = self._hider, None
        if h is not None:
            try:
                h.stop()
            except Exception:
                logger.debug("작업표시줄 감시 정리 실패", exc_info=True)

    def _release_lock(self) -> None:
        if self._holds_lock:
            self._holds_lock = False
            try:
                _BROWSER_LOCK.release()
            except RuntimeError:
                logger.debug("이미 해제된 브라우저 락", exc_info=True)

    def stop(self) -> None:
        d, self.driver = self.driver, None
        self._warmed = False
        self._stop_hider()
        try:
            if d is not None:
                try:
                    d.quit()
                except Exception:
                    logger.debug("브라우저 종료 중 경고 무시", exc_info=True)
        finally:
            self._release_lock()

    def __enter__(self) -> "CoupangScraper":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ── 내부 유틸 ───────────────────────────────────────────────────────
    def _already_on(self, url: str) -> bool:
        """이미 그 페이지를 정상적으로 띄워둔 상태인가?

        같은 URL을 연속으로 두 번 열면 쿠팡이 403을 준다(실측). 그래서
        ``probe()`` 직후 같은 상품을 ``fetch()`` 하는 경우처럼 중복 이동이 생기면
        다시 열지 않고 현재 페이지를 그대로 쓴다.
        """
        try:
            cur = self.driver.current_url or ""
        except Exception:
            return False
        ids = extract_ids(url)
        pid = ids.get("product_id")
        if not pid or f"/vp/products/{pid}" not in cur:
            return False
        return self._page_state()[0] == "ok"

    def _open(self, url: str, reconnect: int = RECONNECT_PAGE) -> None:
        """UC 모드로 페이지를 연다.

        ``uc_open_with_reconnect`` 는 로딩 중에 자동화 연결을 끊어서 Akamai 센서에
        잡히지 않게 한다. 이게 이 프로그램이 동작하는 유일한 이유다.
        """
        try:
            self.driver.uc_open_with_reconnect(url, reconnect)
        except Exception:
            # UC 전용 메서드가 없는 드라이버면 평범하게 연다(성공률은 떨어진다).
            logger.debug("uc_open_with_reconnect 실패 — 일반 get으로 대체", exc_info=True)
            self.driver.get(url)

    def _page_state(self) -> tuple[str, int]:
        """현재 페이지 상태를 (상태명, 길이)로 돌려준다."""
        try:
            html = self.driver.page_source
        except Exception:
            return ("exception", 0)
        n = len(html)
        if "Access Denied" in html and n < 3000:
            return ("access_denied", n)
        if self.selectors.get("challenge_marker", "sec-if-cpt-container") in html:
            return ("challenge", n)
        if self.selectors.get("error_marker", "error403") in html:
            return ("coupang_403", n)
        if n < int(self.selectors.get("min_page_length", 30000)):
            return ("too_short", n)
        return ("ok", n)

    def _wait_ready(self, timeout: int = 45) -> bool:
        """챌린지가 끝나고 진짜 페이지가 뜰 때까지 기다린다."""
        deadline = time.time() + timeout
        state = "unknown"
        while time.time() < deadline:
            state, n = self._page_state()
            if state == "ok":
                self._last_state = "ok"
                return True
            if state in BLOCKED_STATES:
                # 차단 응답은 기다린다고 풀리지 않는다. 45초를 통째로 버리지 말고
                # 즉시 빠져나온다(차단된 사이클이 13분씩 걸리던 원인).
                break
            time.sleep(1.5)
        self._last_state = state
        logger.warning("페이지 준비 실패 (상태=%s)", state)
        return False

    def _warm_up(self) -> None:
        """홈 → 검색 순서로 방문해 신뢰 세션을 만든다.

        **이 경로를 건너뛰고 상품 URL로 바로 가면 반드시 차단된다**(실측).
        """
        try:
            self._open(HOME_URL, RECONNECT_HOME)
            home_ok = self._wait_ready(45)
            time.sleep(random.uniform(2, 4))

            self._open(SEARCH_URL, RECONNECT_PAGE)
            search_ok = self._wait_ready(45)
            time.sleep(random.uniform(2, 4))

            self._warmed = bool(home_ok and search_ok)
            logger.info("세션 워밍업 홈=%s 검색=%s", home_ok, search_ok)
        except Exception:
            logger.warning("세션 워밍업 중 오류 — 계속 진행", exc_info=True)

    def _detect_sold_out(self) -> bool:
        from selenium.webdriver.common.by import By
        try:
            for sel in self.selectors.get("sold_out", []):
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    txt = (el.text or "").strip()
                    if any(k in txt for k in self.selectors.get("sold_out_texts", [])):
                        return True
        except Exception:
            logger.debug("품절 감지 실패", exc_info=True)
        return False

    def _meta(self, selector: str) -> Optional[str]:
        try:
            return self.driver.execute_script(
                "var m=document.querySelector(arguments[0]);return m?m.content:null;", selector
            )
        except Exception:
            return None

    # ── 공개 API ────────────────────────────────────────────────────────
    def fetch(self, url: str) -> PricePoint:
        """상품 페이지에서 가격을 읽는다. 실패해도 예외 대신 price=None을 반환."""
        pid = extract_ids(url).get("product_id") or url
        started = datetime.now()

        if self.driver is None:
            try:
                self.start()
            except Exception:
                logger.exception("브라우저 기동 실패")
                return build_price_point(pid, [], checked_at=started,
                                         extra_note="browser_start_failed")

        # 워밍업(홈→검색)이 실패했다면 이미 차단된 상태다. 그런데도 상품을 하나씩
        # 다 시도하고 실패할 때마다 재워밍업+재시도까지 돌면, 차단당한 순간 요청량이
        # 오히려 3배로 늘어난다(실측: 하루 288회 → 864회). 차단을 더 굳히는 악순환이라
        # 아예 시도하지 않고 즉시 포기한다.
        if not self._warmed:
            logger.warning("워밍업이 실패한 상태 — 차단으로 보고 이번 상품은 건너뜁니다 pid=%s", pid)
            self._blocked = True
            return build_price_point(pid, [], checked_at=started,
                                     extra_note="skipped:not_warmed")

        try:
            if self._already_on(url):
                # probe() 직후 같은 상품을 fetch() 하는 경우 — 다시 열면 403이 난다.
                logger.debug("이미 열려 있는 페이지 재사용 pid=%s", pid)
                ok = True
                self._last_state = "ok"
            else:
                self._open(url, RECONNECT_PAGE)
                ok = self._wait_ready(45)

            # 차단됐으면 워밍업 경로를 다시 태우고 한 번만 재시도한다.
            # (일시적인 403은 이걸로 회복된다 — 실제로 회복 사례를 확인했다)
            if not ok and self._last_state in ("coupang_403", "too_short"):
                logger.info("차단 감지(%s) → 워밍업 후 1회 재시도", self._last_state)
                self._warm_up()
                if not self._warmed:
                    # 재워밍업까지 실패 = 확실히 막혔다. 남은 상품도 볼 필요 없다.
                    logger.warning("재워밍업도 실패 — 이번 사이클은 중단합니다")
                    self._blocked = True
                    return build_price_point(pid, [], checked_at=started,
                                             extra_note="blocked:warmup_failed")
                time.sleep(random.uniform(2, 5))
                self._open(url, RECONNECT_PAGE)
                ok = self._wait_ready(45)

            if not ok:
                if self._last_state in BLOCKED_STATES:
                    self._blocked = True
                return build_price_point(pid, [], checked_at=started,
                                         extra_note=f"page_not_ready:{self._last_state}")

            # 가격 블록이 늦게 그려지는 경우가 있어 잠깐 더 준다.
            time.sleep(random.uniform(1.5, 3.0))

            tokens = self.driver.execute_script(
                _JS_EXTRACT_TOKENS, self.selectors.get("price_container", ".price-container")
            )
            if tokens is None:
                sold = self._detect_sold_out()
                note = "no_price_container" + ("+soldout" if sold else "")
                logger.warning("가격 블록 없음 pid=%s sold_out=%s", pid, sold)
                return build_price_point(pid, [], sold_out=sold, checked_at=started, extra_note=note)

            sold = self._detect_sold_out()
            pp = build_price_point(pid, tokens, sold_out=sold, checked_at=started)
            logger.info(
                "수집 pid=%s 가격=%s 정가=%s 할인=%s%% 재고=%s",
                pid, pp.price, pp.list_price, pp.discount_rate, pp.availability.value,
            )
            return pp
        except Exception:
            logger.exception("상품 수집 실패 url=%s", url)
            return build_price_point(pid, [], checked_at=started, extra_note="fetch_exception")

    def probe(self, url: str) -> Product:
        """신규 등록용. 상품명/이미지 등 메타를 뽑아 Product를 만든다."""
        ids = extract_ids(url)
        pid = ids.get("product_id") or ""
        norm = normalize_url(url)
        name, image = "", None

        if self.driver is None:
            self.start()
        try:
            if self._already_on(url):
                ok = True
                self._last_state = "ok"
            else:
                self._open(url, RECONNECT_PAGE)
                ok = self._wait_ready(45)
            if not ok and self._last_state in ("access_denied", "coupang_403", "too_short"):
                self._warm_up()
                time.sleep(random.uniform(2, 5))
                self._open(url, RECONNECT_PAGE)
                ok = self._wait_ready(45)
            if ok:
                time.sleep(1.5)
                name = clean_title(self._meta(self.selectors.get("og_title", "meta[property='og:title']")))
                image = self._meta(self.selectors.get("og_image", "meta[property='og:image']"))
                if not name:
                    name = clean_title(self.driver.title)
        except Exception:
            logger.exception("상품 메타 조회 실패 url=%s", url)

        return Product(
            product_id=pid,
            url=norm or url,
            name=name,
            item_id=ids.get("item_id"),
            vendor_item_id=ids.get("vendor_item_id"),
            added_at=datetime.now(),
            image_url=image,
        )

    def probe_dict(self, url: str) -> dict[str, Any]:
        """설정 GUI(ui/settings_gui.py)가 기대하는 dict 형태로 돌려주는 어댑터."""
        p = self.probe(url)
        return {
            "product_id": p.product_id,
            "url": p.url,
            "name": p.name,
            "item_id": p.item_id,
            "vendor_item_id": p.vendor_item_id,
            "image_url": p.image_url,
        }


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    URLS = [
        "https://www.coupang.com/vp/products/9684343895?itemId=28960465352&vendorItemId=95890673988",
        "https://www.coupang.com/vp/products/8288998026?itemId=27863137394&vendorItemId=86624554388",
    ]
    with CoupangScraper(headless=False) as sc:
        meta = sc.probe(URLS[0])
        print(f"\n[probe] 상품명: {meta.name}")
        for i, u in enumerate(URLS):
            if i:
                time.sleep(random.uniform(5, 9))
            pp = sc.fetch(u)
            print(f"\n[fetch {i+1}] 가격={pp.price} 정가={pp.list_price} "
                  f"할인율={pp.discount_rate}% 쿠폰가={pp.coupon_price} 재고={pp.availability.value}")
            print(f"           note={pp.raw_note}")
