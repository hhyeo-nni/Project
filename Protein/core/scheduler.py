"""주기 실행 스케줄러.

백그라운드 스레드에서 등록된 상품들을 주기적으로 순회하며 가격을 확인하고,
규칙 엔진(core/rules.py)으로 알림 여부를 판정한 뒤 토스트(core/notifier.py)를
띄우고 이력을 저장(core/store.py)한다.

SPEC.md에는 스케줄러 전용 절이 없어 이 모듈의 동작 요구사항은 작업 지시서
(다른 완성 모듈들의 실제 시그니처 + 스케줄러 요구사항)를 따른다.

**core/scraper.py 계약 (duck typing, import 하지 않음)**
scraper_factory()가 돌려주는 객체는 아래 메서드를 제공해야 한다:
    .start() -> None                 # 브라우저 기동 등 준비. 사이클당 1회만 호출.
    .stop()  -> None                 # 정리. 사이클 종료 시 1회만 호출(성공/실패 무관).
    .fetch(url: str) -> PricePoint   # 실패해도 예외를 던지지 않고
                                      # price=None인 PricePoint를 반환해야 한다.
core/scraper.py는 이 스케줄러 작성 시점에 아직 존재하지 않을 수 있으므로,
이 파일은 core.scraper를 절대 import하지 않는다.

**호출 순서 계약 (중요)**
상품별 처리는 반드시 다음 순서를 지킨다:
    1) scraper.fetch(url)                       -> PricePoint
    2) rules.evaluate(product, pp, store, ...)   -> list[AlertEvent]   (아직 add_price 하기 전!)
    3) store.add_price(pp)
    4) notifier.notify(ev) + store.add_alert(ev) (각 AlertEvent에 대해)

2)를 3)보다 먼저 호출해야 하는 이유: core/rules.py의 LOWEST_EVER 판정은
``store.min_price(product_id)``가 "현재 막 조회한 가격을 제외한" 과거 최저가를
돌려준다는 전제를 깔고 있다(SPEC.md 4절). add_price를 먼저 해버리면 방금
조회한 현재가가 최저가 계산에 섞여 들어가 "현재가 == 최저가"가 되어 최저가
갱신을 영원히 감지하지 못하는 등 오탐/누락이 생긴다. (core/rules.py 자체도
방어적으로 엄격 부등호를 쓰지만, 순서를 지키는 것이 원칙이다.)
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from core.config import AppConfig, product_of, rules_of
from core.models import AlertEvent, Availability, PricePoint
from core.notifier import Notifier
from core.rules import evaluate as evaluate_rules
from core.store import Store

logger = logging.getLogger(__name__)

# stop()/run_once_async() 응답성을 위해 긴 대기를 잘게 쪼개는 단위(초).
# time.sleep(긴시간) 대신 이 단위로 반복 Event.wait()를 호출한다.
_WAIT_STEP_SECONDS = 1.0

# 사이클 전체 실패 시 다음 대기시간에 곱하는 배수의 상한.
_MAX_BACKOFF_MULTIPLIER = 4

# 연속 파싱 실패 이 횟수에 도달하면 1회 알림.
_FAIL_STREAK_THRESHOLD = 3

# 차단이 이어질 때 대기 시간을 늘리는 배수의 상한(30분 주기 기준 최대 4시간).
_MAX_BLOCKED_BACKOFF_MULTIPLIER = 8

# 차단이 이 횟수만큼 연속되면 사용자에게 1회 알린다.
_BLOCKED_NOTIFY_AT = 2


class Scheduler:
    """상품 가격을 주기적으로 점검하고 알림을 발생시키는 백그라운드 스케줄러."""

    def __init__(
        self,
        store: Store,
        notifier: Notifier,
        config: AppConfig,
        scraper_factory: Callable[[], Any],
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._config = config
        self._scraper_factory = scraper_factory

        self._thread: Optional[threading.Thread] = None

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()     # set == 일시정지 상태
        self._run_now_event = threading.Event()   # set == 즉시 1회 실행 요청

        self._next_run_at: Optional[datetime] = None
        self._last_run_at: Optional[datetime] = None

        self._backoff_multiplier = 1

        # 상품별 연속 파싱 실패 카운터 (product_id -> 연속 실패 횟수)
        self._fail_streaks: dict[str, int] = {}

        # 직전 사이클이 쿠팡 차단으로 끝났는가 / 연속 몇 번째인가.
        # 차단이 이어지면 대기 시간을 크게 늘려 평판이 회복될 시간을 준다.
        self._blocked_cycle = False
        self._blocked_streak = 0

    # ------------------------------------------------------------------
    # 상태 조회
    # ------------------------------------------------------------------
    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def next_run_at(self) -> Optional[datetime]:
        return self._next_run_at

    @property
    def last_run_at(self) -> Optional[datetime]:
        return self._last_run_at

    # ------------------------------------------------------------------
    # 스레드 제어
    # ------------------------------------------------------------------
    def start(self) -> None:
        """백그라운드 데몬 스레드를 기동한다."""
        if self.is_running:
            logger.warning("스케줄러가 이미 실행 중입니다. start() 무시.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="ProteinSchedulerThread", daemon=True
        )
        self._thread.start()
        logger.info("스케줄러 스레드를 시작했습니다.")

    def stop(self) -> None:
        """정지를 요청하고 스레드가 끝날 때까지 짧게 대기한다.

        긴 대기(_wait)는 _WAIT_STEP_SECONDS 단위로 stop_event를 확인하므로
        5초 이내에 스레드가 종료되어야 한다.
        """
        logger.info("스케줄러 정지를 요청합니다.")
        self._stop_event.set()
        # 대기 중인 루프(사이클 간 대기, pause 대기)를 즉시 깨운다.
        self._run_now_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("스케줄러 스레드가 5초 내에 종료되지 않았습니다.")
        logger.info("스케줄러 정지 처리 완료.")

    def pause(self) -> None:
        self._pause_event.set()
        logger.info("스케줄러를 일시정지했습니다.")

    def resume(self) -> None:
        self._pause_event.clear()
        self._run_now_event.set()  # 재개 즉시 다음 사이클로 넘어가도록 깨움
        logger.info("스케줄러를 재개했습니다.")

    def run_once_async(self) -> None:
        """대기 중인 루프를 즉시 깨워 1회 사이클을 실행시킨다(비블로킹)."""
        logger.info("즉시 점검 요청(run_once_async).")
        self._run_now_event.set()

    # ------------------------------------------------------------------
    # 내부 루프
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        """스레드 본체. 어떤 예외가 나도 스레드가 죽지 않도록 최상위에서 감싼다."""
        try:
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    # 일시정지 중: 사이클을 돌리지 않되 스레드는 살아있는다.
                    # run_now_event가 와도(예: resume()) 여기서는 그냥 소비하고
                    # pause 상태를 다시 확인한다.
                    self._wait(_WAIT_STEP_SECONDS)
                    continue

                success = True
                try:
                    self.check_all()
                except Exception:
                    logger.exception(
                        "스케줄러 사이클 실행 중 예외 발생 — 다음 주기에 재시도합니다."
                    )
                    success = False

                self._last_run_at = datetime.now()

                if not success:
                    self._backoff_multiplier = min(
                        self._backoff_multiplier * 2, _MAX_BACKOFF_MULTIPLIER
                    )
                elif self._blocked_cycle:
                    # 차단은 "실패"와 다르게 다뤄야 한다. 30분 뒤에 또 두드리면
                    # 차단이 계속 갱신될 뿐이라, 훨씬 길게(최대 8배) 쉬어 준다.
                    self._blocked_streak += 1
                    self._backoff_multiplier = min(
                        2 ** self._blocked_streak, _MAX_BLOCKED_BACKOFF_MULTIPLIER
                    )
                    logger.warning(
                        "차단 %d회 연속 — 다음 점검까지 평소의 %d배를 쉽니다.",
                        self._blocked_streak, self._backoff_multiplier,
                    )
                    if self._blocked_streak == _BLOCKED_NOTIFY_AT:
                        try:
                            self._notifier.notify_error(
                                "쿠팡 접속이 막혔습니다",
                                "잠시 수집을 쉬었다가 다시 시도합니다. "
                                "계속되면 확인 주기를 늘리거나 잠시 후 다시 실행해 주세요.",
                            )
                        except Exception:
                            logger.exception("차단 알림 전송 실패")
                else:
                    self._backoff_multiplier = 1
                    self._blocked_streak = 0

                if self._stop_event.is_set():
                    break

                self._sleep_until_next_cycle()
        except Exception:
            logger.exception("스케줄러 스레드에서 예기치 못한 최상위 예외 발생.")
        finally:
            logger.info("스케줄러 스레드가 종료됩니다.")

    def _sleep_until_next_cycle(self) -> None:
        """다음 사이클까지 대기한다. next_run_at을 갱신하고, stop/run_now에 반응한다."""
        base_minutes = max(self._config.poll_interval_minutes, 0.0)
        jitter = max(self._config.jitter_minutes, 0.0)
        jittered_minutes = base_minutes + random.uniform(-jitter, jitter) if jitter else base_minutes
        jittered_minutes = max(jittered_minutes, 0.001)  # 0/음수 방지(즉시 재실행 폭주 방지)

        wait_seconds = jittered_minutes * 60.0 * self._backoff_multiplier

        self._next_run_at = datetime.now() + timedelta(seconds=wait_seconds)
        logger.info(
            "다음 점검 예정 시각: %s (대기 약 %.1f초, backoff x%d)",
            self._next_run_at,
            wait_seconds,
            self._backoff_multiplier,
        )

        self._run_now_event.clear()
        self._wait(wait_seconds)

    def _wait(self, seconds: float) -> None:
        """stop_event/run_now_event에 반응하며 짧은 단위로 대기한다.

        time.sleep(긴시간)을 절대 쓰지 않는다 — stop()이 최대 _WAIT_STEP_SECONDS
        지연 내에 감지되어야 한다.
        """
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while True:
            if self._stop_event.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            step = min(_WAIT_STEP_SECONDS, remaining)
            # run_now_event가 set되면 즉시 깨어나 대기를 끝낸다(지금 확인 / 재개).
            if self._run_now_event.wait(timeout=step):
                self._run_now_event.clear()
                return

    # ------------------------------------------------------------------
    # 점검 로직 (동기, 테스트/수동 호출 가능)
    # ------------------------------------------------------------------
    def check_all(self) -> list[AlertEvent]:
        """활성화된 모든 상품을 1회 순차 점검한다.

        scraper를 1번만 start()/stop()하고, 상품 사이에는 config의
        request_delay_seconds 범위 내 랜덤 지연을 둔다(첫 상품 앞에는 지연 없음).
        """
        all_events: list[AlertEvent] = []
        self._blocked_cycle = False

        entries = [e for e in self._config.products if e.get("enabled", True)]
        if not entries:
            logger.info("점검할 활성화된 상품이 없습니다.")
            return all_events

        scraper = self._scraper_factory()

        try:
            scraper.start()
        except Exception:
            logger.exception("스크래퍼 시작(start) 실패 — 이번 사이클을 중단합니다.")
            raise

        try:
            for i, entry in enumerate(entries):
                if self._stop_event.is_set():
                    logger.info("정지 요청 감지 — 남은 상품 점검을 중단합니다.")
                    break

                if i > 0:
                    delay_min, delay_max = self._config.request_delay_seconds
                    if delay_max < delay_min:
                        delay_min, delay_max = delay_max, delay_min
                    delay = random.uniform(delay_min, delay_max)
                    logger.debug("다음 상품 조회 전 %.1f초 대기.", delay)
                    self._wait(delay)
                    if self._stop_event.is_set():
                        break

                try:
                    events = self._check_one_product(scraper, entry)
                    all_events.extend(events)
                except Exception:
                    logger.exception(
                        "상품 점검 중 예외 발생(다음 상품으로 계속): product_id=%s",
                        entry.get("product_id"),
                    )

                # 쿠팡이 이 세션을 막았다면 남은 상품을 두드려봐야 전부 실패한다.
                # 오히려 요청량만 몇 배로 늘어 차단이 더 굳어지므로 즉시 중단한다.
                if getattr(scraper, "is_blocked", False):
                    self._blocked_cycle = True
                    skipped = len(entries) - (i + 1)
                    logger.warning(
                        "쿠팡 차단 감지 — 이번 사이클을 중단합니다 (남은 상품 %d개 건너뜀).",
                        skipped,
                    )
                    break
        finally:
            try:
                scraper.stop()
            except Exception:
                logger.exception("스크래퍼 종료(stop) 실패.")

        return all_events

    def _check_one_product(self, scraper: Any, entry: dict) -> list[AlertEvent]:
        product = product_of(entry)
        alert_rules = rules_of(entry)

        # config.json을 손으로 편집해 상품을 추가한 경우 DB에는 그 상품이 없다.
        # 그러면 가격 이력 조회(설정 창)와 알림 이력의 상품명 복원이 깨지므로
        # 매 사이클 DB와 동기화해 둔다. (upsert라 중복 걱정 없음)
        try:
            self._store.upsert_product(product)
        except Exception:
            logger.exception("상품 동기화(upsert_product) 실패: product_id=%s", product.product_id)

        pp = scraper.fetch(product.url)

        # 차단(쿠팡이 세션을 막음)은 파싱 실패가 아니다. 이걸 실패로 세면
        # "가격 파싱 실패" 오류 알림이 차단 알림과 겹쳐 사용자를 혼란스럽게 한다.
        # (품절을 실패로 세지 않는 것과 같은 이유)
        if not getattr(scraper, "is_blocked", False):
            self._track_fetch_result(product.product_id, product.name, pp)

        # --- 순서 중요: evaluate가 add_price보다 먼저 ---
        # (모듈 상단 docstring "호출 순서 계약" 참고 — LOWEST_EVER가
        #  현재 가격을 포함하지 않은 store.min_price()를 전제로 판정하기 때문)
        try:
            events = evaluate_rules(
                product,
                pp,
                self._store,
                alert_rules,
                cooldown_hours=self._config.cooldown_hours,
                renotify_on_lower=self._config.renotify_on_lower,
            )
        except Exception:
            logger.exception("규칙 평가(evaluate) 중 예외 발생: product_id=%s", product.product_id)
            events = []

        try:
            self._store.add_price(pp)
        except Exception:
            logger.exception("가격 저장(add_price) 실패: product_id=%s", product.product_id)

        for ev in events:
            try:
                self._notifier.notify(ev)
            except Exception:
                logger.exception("알림 전송(notify) 실패: %s", ev)
            try:
                self._store.add_alert(ev)
            except Exception:
                logger.exception("알림 이력 저장(add_alert) 실패: %s", ev)

        return events

    def _track_fetch_result(self, product_id: str, product_name: str, pp: PricePoint) -> None:
        """연속 파싱 실패 카운터를 관리하고, 3회 연속이면 1회 알림.

        주의: **품절 상품은 실패가 아니다.** 쿠팡은 품절이면 가격 블록 자체를
        렌더링하지 않아 price가 None이 된다. 이걸 파싱 실패로 세면 정상적으로
        품절을 감지한 상품마다 "가격 파싱 실패" 오류 알림이 날아간다.
        (재입고 알림을 기다리는 상품이 바로 이 경우다)
        """
        price = pp.price
        if pp.availability == Availability.OUT_OF_STOCK:
            # 품절은 정상적인 수집 결과이므로 실패 연속 카운터를 초기화한다.
            self._fail_streaks.pop(product_id, None)
            logger.info("품절 상태로 확인됨: %s", product_name)
            return
        if price is None:
            streak = self._fail_streaks.get(product_id, 0) + 1
            self._fail_streaks[product_id] = streak
            logger.warning("가격 파싱 실패(연속 %d회): %s", streak, product_name)
            if streak == _FAIL_STREAK_THRESHOLD:
                try:
                    self._notifier.notify_error(
                        "가격 파싱 실패",
                        f"'{product_name}' 가격을 {streak}회 연속으로 가져오지 못했습니다. "
                        f"selectors.json 또는 사이트 구조 변경을 확인해 주세요.",
                    )
                except Exception:
                    logger.exception("파싱 실패 알림(notify_error) 전송 중 예외 발생.")
        else:
            if self._fail_streaks.get(product_id):
                logger.info("가격 파싱 성공, 연속 실패 카운터 리셋: %s", product_name)
            self._fail_streaks[product_id] = 0


if __name__ == "__main__":
    # 간이 자체 테스트: 진짜 Store + 가짜 scraper/notifier로 check_all() 1회 검증.
    # (더 상세한 시나리오별 검증은 스크래치패드의 별도 테스트 스크립트 참고.)
    import shutil
    import sys
    import tempfile

    from core.models import Availability, PricePoint

    class _FakeScraper:
        def __init__(self) -> None:
            self.started = False
            self.fetch_calls: list[str] = []

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.started = False

        def fetch(self, url: str) -> PricePoint:
            self.fetch_calls.append(url)
            return PricePoint(
                product_id="p1",
                checked_at=datetime.now(),
                price=39900,
                list_price=52900,
                discount_rate=25,
                coupon_price=None,
                wow_price=None,
                availability=Availability.IN_STOCK,
                raw_note="self-test",
            )

    class _FakeNotifier:
        def __init__(self) -> None:
            self.notified: list[AlertEvent] = []
            self.errors: list[tuple[str, str]] = []

        def notify(self, ev: AlertEvent) -> None:
            self.notified.append(ev)

        def notify_error(self, title: str, message: str) -> None:
            self.errors.append((title, message))

        def notify_info(self, title: str, message: str) -> None:
            pass

    from pathlib import Path

    tmp_dir = Path(tempfile.mkdtemp(prefix="protein_scheduler_test_"))
    try:
        db_path = tmp_dir / "test.db"
        test_store = Store(db_path)

        test_cfg = AppConfig(
            poll_interval_minutes=30,
            jitter_minutes=5,
            cooldown_hours=12,
            renotify_on_lower=True,
            sound=False,
            request_delay_seconds=(0, 0),
            headless=True,
            products=[
                {
                    "product_id": "p1",
                    "url": "https://www.coupang.com/vp/products/p1?itemId=1&vendorItemId=2",
                    "name": "셀프테스트 프로틴",
                    "protein_grams": 800.0,
                    "enabled": True,
                    "rules": [
                        {"kind": "target_price", "threshold": 45000, "enabled": True},
                    ],
                }
            ],
        )

        fake_notifier = _FakeNotifier()
        scheduler = Scheduler(
            store=test_store,
            notifier=fake_notifier,  # type: ignore[arg-type]
            config=test_cfg,
            scraper_factory=_FakeScraper,
        )

        events = scheduler.check_all()
        assert len(events) == 1, f"target_price 알림 1건이 나와야 함, got {len(events)}"
        assert events[0].kind.value == "target_price"
        assert test_store.latest_price("p1") is not None
        assert test_store.latest_price("p1").price == 39900
        assert len(fake_notifier.notified) == 1

        assert scheduler.is_running is False
        assert scheduler.is_paused is False

        test_store.close()
        print("OK: scheduler.py self-test passed", file=sys.stderr)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
