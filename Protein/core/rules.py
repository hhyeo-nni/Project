"""알림 규칙 판정 엔진 (SPEC.md 4절).

핵심 전제(호출 계약)
--------------------
``evaluate()``는 반드시 **현재 가격(current)을 store에 저장하기 전**에 호출되어야
한다. 특히 LOWEST_EVER 규칙은 ``store.min_price(pid)``가 "현재 기록을 제외한"
과거 최저가를 돌려준다는 전제(SPEC 4절)를 따르되, 만약 호출 순서가 지켜지지
않아 ``min_price``가 이미 현재가를 포함해버린 경우에도 오탐(false positive)이
나지 않도록 방어적으로 ``current.price < prev_min`` 엄격 부등호를 사용한다.
(``min_price``에 현재가가 이미 포함돼 있다면 ``prev_min <= current.price``가
되어 자연히 조건이 거짓이 되므로 안전하다.)

그 외 로직은 SPEC 4절 표를 그대로 따른다:

- ``current.price is None`` (파싱 실패) → 빈 리스트 반환.
- ``current.availability == OUT_OF_STOCK`` → RESTOCK 외 전부 스킵.
- ``rules``에서 ``enabled=False``인 항목은 건너뜀.
- 쿨다운: ``store.last_alert(pid, kind)``가 ``cooldown_hours`` 이내면 억제.
  단 ``renotify_on_lower=True``이고 ``current.price < last_alert.price``면
  억제를 해제한다.
- 개별 규칙 판정 중 예외가 발생하면 로그만 남기고 해당 규칙만 건너뛴다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from core.models import AlertEvent, AlertRule, Availability, PricePoint, Product, RuleKind
from core.store import Store

logger = logging.getLogger(__name__)

# 규칙 중요도 순 (SPEC에 명시된 정렬 순서)
_KIND_ORDER: dict[RuleKind, int] = {
    RuleKind.LOWEST_EVER: 0,
    RuleKind.TARGET_PRICE: 1,
    RuleKind.UNIT_PRICE: 2,
    RuleKind.DISCOUNT_SPIKE: 3,
    RuleKind.DISCOUNT_RATE: 4,
    RuleKind.RESTOCK: 5,
}


def _won(v: float) -> str:
    """정수 원화 금액을 천단위 콤마 문자열로 변환."""
    return f"{int(round(v)):,}원"


def _is_in_cooldown(
    store: Store,
    product_id: str,
    kind: RuleKind,
    current: PricePoint,
    cooldown_hours: int,
    renotify_on_lower: bool,
) -> bool:
    """이 규칙이 쿨다운으로 억제되어야 하면 True."""
    last = store.last_alert(product_id, kind)
    if last is None:
        return False

    elapsed = current.checked_at - last.fired_at
    if elapsed >= timedelta(hours=cooldown_hours):
        return False  # 쿨다운 기간이 지남 → 억제 안 함

    # 쿨다운 기간 내부. renotify_on_lower 조건 확인.
    if renotify_on_lower and current.price is not None and current.price < last.price:
        return False  # 억제 해제

    return True


def _check_target_price(
    product: Product, current: PricePoint, store: Store, rule: AlertRule
) -> Optional[tuple[str, int]]:
    if current.price is not None and current.price <= rule.threshold:
        # 주의: evaluate()는 현재 가격을 저장하기 **전에** 호출된다(모듈 docstring 참고).
        # 따라서 DB에 들어있는 가장 최근 행이 곧 '직전 확인'이다.
        # previous_price()는 OFFSET 1이라 한 칸 더 과거를 보므로 쓰면 안 된다.
        prev = store.latest_price(product.product_id)
        prev_price = prev.price if prev is not None else None
        # 이전가가 있고 실제로 "하락"한 경우에만 "이전가 → 현재가 (▼N%)" 형식을
        # 쓴다. 가격이 그대로거나(0% 하락) 올랐다면 화살표가 어색하므로 뺀다.
        if prev_price is not None and prev_price > current.price:
            pct = round((1 - current.price / prev_price) * 100)
            msg = (
                f"{_won(prev_price)} → {_won(current.price)} (▼{pct}%) · "
                f"목표가 {_won(rule.threshold)} 도달"
            )
        else:
            msg = f"{_won(current.price)} · 목표가 {_won(rule.threshold)} 도달"
        return msg, current.price
    return None


def _check_discount_rate(
    product: Product, current: PricePoint, store: Store, rule: AlertRule
) -> Optional[tuple[str, int]]:
    if current.discount_rate is not None and current.discount_rate >= rule.threshold:
        msg = (
            f"{_won(current.price)} · 할인율 {current.discount_rate}% "
            f"(기준 {int(rule.threshold)}% 이상)"
        )
        return msg, current.price
    return None


def _check_discount_spike(
    product: Product, current: PricePoint, store: Store, rule: AlertRule
) -> Optional[tuple[str, int]]:
    if current.discount_rate is None:
        return None
    avg = store.avg_discount_rate(product.product_id, 7)
    if avg is not None and (current.discount_rate - avg) >= rule.threshold:
        spike = current.discount_rate - avg
        msg = (
            f"{_won(current.price)} · 할인율 {current.discount_rate}% — "
            f"최근 7일 평균 {avg:.0f}%보다 {spike:.0f}%p 급등"
        )
        return msg, current.price
    return None


def _check_lowest_ever(
    product: Product, current: PricePoint, store: Store, rule: AlertRule
) -> Optional[tuple[str, int]]:
    prev_min = store.min_price(product.product_id)
    if prev_min is not None and current.price is not None and current.price < prev_min:
        msg = f"{_won(current.price)} · 역대 최저가 갱신! (이전 최저 {_won(prev_min)})"
        return msg, current.price
    return None


def _check_restock(
    product: Product, current: PricePoint, store: Store, rule: AlertRule
) -> Optional[tuple[str, int]]:
    # 직전 확인 시점의 재고 상태와 비교해야 하므로 latest_price()를 쓴다.
    # (evaluate()는 현재 가격 저장 전에 불리므로 latest_price()가 곧 직전 확인이다)
    prev = store.latest_price(product.product_id)
    if (
        prev is not None
        and prev.availability == Availability.OUT_OF_STOCK
        and current.availability == Availability.IN_STOCK
    ):
        msg = f"재입고! {_won(current.price)}에 판매 재개"
        return msg, current.price
    return None


def _check_unit_price(
    product: Product, current: PricePoint, store: Store, rule: AlertRule
) -> Optional[tuple[str, int]]:
    if product.protein_grams and product.protein_grams > 0 and current.price is not None:
        unit = current.price / product.protein_grams
        if unit <= rule.threshold:
            msg = (
                f"{_won(current.price)} · 단백질 1g당 {unit:.1f}원 "
                f"(기준 {int(rule.threshold)}원 이하)"
            )
            return msg, current.price
    return None


_CHECKERS = {
    RuleKind.TARGET_PRICE: _check_target_price,
    RuleKind.DISCOUNT_RATE: _check_discount_rate,
    RuleKind.DISCOUNT_SPIKE: _check_discount_spike,
    RuleKind.LOWEST_EVER: _check_lowest_ever,
    RuleKind.RESTOCK: _check_restock,
    RuleKind.UNIT_PRICE: _check_unit_price,
}


def _build_detail(
    product: Product,
    current: PricePoint,
    prev_price: Optional[int],
    message: str = "",
) -> str:
    """토스트 셋째 줄에 넣을 부연 설명을 만든다.

    "왜 이 알림이 떴는지"를 사용자가 한눈에 판단할 수 있도록 정가·할인율·직전
    확인가·단백질 단가 같은 맥락을 붙여준다. 없는 정보는 조용히 건너뛴다.
    """
    bits: list[str] = []
    try:
        if current.list_price and current.discount_rate is not None:
            bits.append(f"정가 {current.list_price:,}원에서 {current.discount_rate}% 할인")
        elif current.list_price:
            bits.append(f"정가 {current.list_price:,}원")

        if current.coupon_price:
            bits.append("쿠폰 적용가")

        # 본문에 이미 '이전가 → 현재가' 가 들어간 경우엔 중복이라 생략한다.
        if (
            prev_price is not None
            and current.price is not None
            and prev_price != current.price
            and "→" not in message
        ):
            diff = current.price - prev_price
            arrow = "▼" if diff < 0 else "▲"
            bits.append(f"직전 확인 {prev_price:,}원 ({arrow}{abs(diff):,}원)")

        if product.protein_grams and current.price is not None and product.protein_grams > 0:
            bits.append(f"단백질 1g당 {current.price / product.protein_grams:.1f}원")

        if current.availability == Availability.OUT_OF_STOCK:
            bits.append("품절")
    except Exception:  # 부연 설명 때문에 알림이 죽으면 안 된다
        logger.exception("알림 상세 문구 생성 실패: product_id=%s", product.product_id)
        return ""
    return " · ".join(bits)


def evaluate(
    product: Product,
    current: PricePoint,
    store: Store,
    rules: list[AlertRule],
    cooldown_hours: int = 12,
    renotify_on_lower: bool = True,
) -> list[AlertEvent]:
    """SPEC 4절 규칙 엔진. 상단 docstring의 호출 순서 전제를 반드시 지킬 것."""
    if current.price is None:
        return []

    out_of_stock = current.availability == Availability.OUT_OF_STOCK

    prev_price_point = None
    try:
        # evaluate()는 현재 가격 저장 전에 호출되므로 latest_price()가 '직전 확인'이다.
        prev_price_point = store.latest_price(product.product_id)
    except Exception:
        logger.exception(
            "이전 가격 조회 실패: product_id=%s", product.product_id
        )
    prev_price = prev_price_point.price if prev_price_point is not None else None

    events: list[AlertEvent] = []

    for rule in rules:
        if not rule.enabled:
            continue
        if out_of_stock and rule.kind != RuleKind.RESTOCK:
            continue

        checker = _CHECKERS.get(rule.kind)
        if checker is None:
            logger.warning("알 수 없는 규칙 종류: %s", rule.kind)
            continue

        try:
            result = checker(product, current, store, rule)
        except Exception:
            logger.exception(
                "규칙 판정 중 예외 발생: product_id=%s kind=%s",
                product.product_id,
                rule.kind,
            )
            continue

        if result is None:
            continue

        message, price = result

        try:
            suppressed = _is_in_cooldown(
                store, product.product_id, rule.kind, current, cooldown_hours, renotify_on_lower
            )
        except Exception:
            logger.exception(
                "쿨다운 판정 중 예외 발생: product_id=%s kind=%s",
                product.product_id,
                rule.kind,
            )
            continue

        if suppressed:
            continue

        events.append(
            AlertEvent(
                product_id=product.product_id,
                product_name=product.name,
                kind=rule.kind,
                fired_at=current.checked_at,
                price=price,
                prev_price=prev_price,
                message=message,
                url=product.url,
                detail=_build_detail(product, current, prev_price, message),
            )
        )

    events.sort(key=lambda ev: _KIND_ORDER.get(ev.kind, 99))
    return events


if __name__ == "__main__":
    # 간단한 자체 테스트 (진짜 검증은 스크래치패드의 FakeStore 테스트 참고)
    print("core/rules.py: evaluate() 로드 확인 OK")
