"""공용 타입 — 단일 진실 공급원 (SPEC.md 1절).

여기 정의된 타입 시그니처는 한 글자도 바꾸지 않는다. 다른 모듈은 이 타입들을
그대로 가져다 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Availability(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


class RuleKind(str, Enum):
    TARGET_PRICE = "target_price"       # A. 목표가 이하
    DISCOUNT_RATE = "discount_rate"     # B. 할인율 N% 이상
    DISCOUNT_SPIKE = "discount_spike"   # B'. 최근 평균 대비 +N%p 급등
    LOWEST_EVER = "lowest_ever"         # C. 최저가 갱신
    RESTOCK = "restock"                 # D. 재입고
    UNIT_PRICE = "unit_price"           # E. 단백질 1g당 단가 이하


@dataclass
class Product:
    product_id: str                 # 쿠팡 productId (URL의 /vp/products/{id})
    url: str                        # 정규화된 상품 URL (itemId/vendorItemId 포함)
    name: str
    item_id: Optional[str] = None
    vendor_item_id: Optional[str] = None
    protein_grams: Optional[float] = None   # 총 단백질 g (UNIT_PRICE 규칙용, 사용자 입력)
    enabled: bool = True
    added_at: Optional[datetime] = None
    image_url: Optional[str] = None


@dataclass
class PricePoint:
    product_id: str
    checked_at: datetime
    price: Optional[int]            # 최종 실구매가(원). 쿠폰가 있으면 쿠폰가. 파싱 실패 시 None
    list_price: Optional[int]       # 정가(할인 전). 없으면 None
    discount_rate: Optional[int]    # 할인율 % (정수). 없으면 None
    coupon_price: Optional[int]     # 쿠폰 적용가. 없으면 None
    wow_price: Optional[int]        # 와우회원가. 없으면 None
    availability: Availability = Availability.UNKNOWN
    raw_note: str = ""              # 파싱 디버그용 메모


@dataclass
class AlertRule:
    kind: RuleKind
    threshold: float                # TARGET_PRICE:원 / DISCOUNT_*:% / UNIT_PRICE:원per g / RESTOCK,LOWEST_EVER: 무시(0)
    enabled: bool = True


@dataclass
class AlertEvent:
    product_id: str
    product_name: str
    kind: RuleKind
    fired_at: datetime
    price: int
    prev_price: Optional[int]
    message: str                    # 토스트 본문 (핵심 한 줄)
    url: str
    detail: str = ""                # 토스트 셋째 줄 (정가·할인율·직전가 등 부연 설명)


if __name__ == "__main__":
    # 자체 테스트: 타입들이 정상적으로 생성/직렬화 가능한지 확인
    p = Product(product_id="123", url="https://example.com/123", name="테스트 상품")
    assert p.enabled is True
    assert p.item_id is None

    pp = PricePoint(
        product_id="123",
        checked_at=datetime.now(),
        price=45000,
        list_price=52900,
        discount_rate=15,
        coupon_price=None,
        wow_price=None,
    )
    assert pp.availability == Availability.UNKNOWN
    assert pp.availability == "unknown"  # str Enum 비교

    rule = AlertRule(kind=RuleKind.TARGET_PRICE, threshold=45000)
    assert rule.enabled is True
    assert rule.kind == "target_price"

    ev = AlertEvent(
        product_id="123",
        product_name="테스트 상품",
        kind=RuleKind.LOWEST_EVER,
        fired_at=datetime.now(),
        price=39900,
        prev_price=42000,
        message="39,900원 · 역대 최저가 갱신! (이전 최저 42,000원)",
        url="https://example.com/123",
    )
    assert ev.kind == RuleKind.LOWEST_EVER

    # RuleKind 전체 값 확인
    expected_kinds = {
        "target_price", "discount_rate", "discount_spike",
        "lowest_ever", "restock", "unit_price",
    }
    assert {k.value for k in RuleKind} == expected_kinds

    expected_avail = {"in_stock", "out_of_stock", "unknown"}
    assert {a.value for a in Availability} == expected_avail

    print("OK: models.py self-test passed")
