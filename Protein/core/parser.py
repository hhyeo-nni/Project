"""쿠팡 상품 페이지 가격 파서.

## 설계 의도

쿠팡은 상품 페이지를 Tailwind 기반(`twc-*`)으로 개편했고, 클래스명이 자주 바뀐다.
그래서 이 파서는 **클래스명에 의존하지 않는다.** 대신 브라우저에서 뽑아낸
"텍스트 조각 + 계산된 스타일(computed style)" 목록을 받아, 아래 시각적 규칙으로 분류한다.

실제 DOM(2026-09 기준 `.price-container` 내부)::

    42%              ← 할인율 뱃지
    45,900원         ← 최종 판매가 (폰트 22px, 취소선 없음)   ★ 우리가 원하는 값
    (10g당 153원)    ← 단위 단가 (괄호로 감싸짐)
    79,900원         ← 정가 (취소선)
    1,000원 할인 46,900원  ← 쿠폰 할인 (46,900도 취소선)

분류 규칙:
  - 할인율   : ``^\\d+%$`` 패턴
  - 정가     : 취소선(line-through)이 걸린 금액 중 **가장 큰 값**
               (쿠폰 적용 전 금액도 취소선이라 두 개가 잡힌다)
  - 최종가   : 취소선 없고 괄호 안이 아닌 금액 중 **폰트가 가장 큰 값**
  - 단위단가 : ``(10g당 153원)`` 처럼 괄호 + "N당" 패턴
  - 쿠폰     : "할인" 텍스트가 있고 취소선 금액이 2개 이상이면 쿠폰 적용된 것으로 판단

이 규칙은 폰트 크기·취소선이라는 **시각적 의미**에 기대므로, 쿠팡이 클래스명을
바꿔도 레이아웃을 통째로 갈아엎지 않는 한 계속 동작한다.

파싱에 실패하면 예외를 던지지 않고 ``price=None`` 인 PricePoint를 돌려준다.
(rules.py 는 price가 None이면 어떤 알림도 내지 않는다)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Iterable, Optional

from core.models import Availability, PricePoint

logger = logging.getLogger(__name__)

# ── 정규식 ───────────────────────────────────────────────────────────────
_RE_MONEY = re.compile(r"([\d,]+)\s*원")
_RE_RATE = re.compile(r"^\s*(\d{1,2})\s*%\s*$")
# "(10g당 153원)", "(100g당 1,234원)", "(1개당 500원)", "(10g 262원)" — '당'은 선택
_RE_UNIT = re.compile(r"\(\s*([\d,]+(?:\.\d+)?)\s*([a-zA-Z가-힣]+?)\s*당?\s*([\d,]+)\s*원\s*\)")

_WOW_KEYWORDS = ("와우", "로켓와우")
_COUPON_KEYWORDS = ("할인", "쿠폰")


def _to_int(s: str) -> Optional[int]:
    """'45,900' → 45900. 실패하면 None."""
    try:
        return int(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _money_in(text: str) -> list[int]:
    """텍스트에 들어있는 모든 '원' 금액을 정수 리스트로."""
    out = []
    for m in _RE_MONEY.finditer(text or ""):
        v = _to_int(m.group(1))
        if v is not None:
            out.append(v)
    return out


def parse_unit_price(text: str) -> Optional[tuple[float, str, int]]:
    """'(10g당 153원)' → (10.0, 'g', 153). 못 찾으면 None."""
    m = _RE_UNIT.search(text or "")
    if not m:
        return None
    qty = _to_int(m.group(1).replace(".", "")) if "." not in m.group(1) else None
    try:
        qty_f = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    won = _to_int(m.group(3))
    if won is None or qty_f <= 0:
        return None
    return (qty_f, m.group(2), won)


def won_per_gram(text: str) -> Optional[float]:
    """'(10g당 153원)' → 15.3 (1g당 원). g 단위가 아니면 None."""
    parsed = parse_unit_price(text)
    if not parsed:
        return None
    qty, unit, won = parsed
    if unit.lower() not in ("g", "그램"):
        if unit.lower() in ("kg", "킬로그램"):
            return won / (qty * 1000.0)
        return None
    return won / qty


# ── 핵심: 토큰 분류 ──────────────────────────────────────────────────────
def classify_tokens(tokens: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """브라우저에서 뽑은 텍스트 토큰들을 가격 필드로 분류한다.

    각 토큰은 ``{"t": 텍스트, "fs": 폰트크기(float), "lt": 취소선여부(bool)}`` 형태.
    브라우저 없이도 호출 가능하므로 단위 테스트가 쉽다.

    반환: ``{price, list_price, discount_rate, coupon_price, wow_price,
             unit_price_won_per_g, note}``
    """
    toks = [t for t in tokens if (t.get("t") or "").strip()]

    rate: Optional[int] = None
    unit_wpg: Optional[float] = None
    struck: list[int] = []       # 취소선 금액들
    plain: list[tuple[float, int]] = []  # (폰트크기, 금액) — 취소선 없는 금액
    has_coupon_word = False
    has_wow_word = False

    for tk in toks:
        text = (tk.get("t") or "").strip()
        try:
            fs = float(tk.get("fs") or 0)
        except (TypeError, ValueError):
            fs = 0.0
        lt = bool(tk.get("lt"))

        if any(k in text for k in _WOW_KEYWORDS):
            has_wow_word = True
        if any(k in text for k in _COUPON_KEYWORDS):
            has_coupon_word = True

        # 할인율 뱃지
        m = _RE_RATE.match(text)
        if m and rate is None:
            rate = _to_int(m.group(1))
            continue

        # 단위 단가는 괄호로 감싸여 있다 — 금액 후보에서 반드시 제외
        wpg = won_per_gram(text)
        if wpg is not None:
            unit_wpg = wpg
            continue
        if _RE_UNIT.search(text):
            continue

        for amount in _money_in(text):
            if lt:
                struck.append(amount)
            else:
                plain.append((fs, amount))

    # 최종가: 취소선 없는 금액 중 폰트가 가장 큰 것
    price: Optional[int] = None
    if plain:
        price = max(plain, key=lambda p: (p[0], p[1]))[1]

    # 정가: 취소선 금액 중 가장 큰 것 (쿠폰 적용 전 금액도 취소선이라 둘 다 잡힘)
    list_price: Optional[int] = max(struck) if struck else None

    # 쿠폰: 취소선 금액이 2개 이상이고 '할인' 문구가 있으면 쿠폰이 적용된 상태
    coupon_price: Optional[int] = None
    if price is not None and has_coupon_word and len(struck) >= 2:
        coupon_price = price

    wow_price: Optional[int] = price if (has_wow_word and price is not None) else None

    # 할인율이 화면에 없으면 정가/판매가로 계산
    if rate is None and price is not None and list_price and list_price > price:
        rate = round((list_price - price) / list_price * 100)

    # 정가가 판매가보다 작으면 뭔가 잘못 잡은 것 — 버린다
    if list_price is not None and price is not None and list_price < price:
        list_price = None

    note_bits = [f"tokens={len(toks)}", f"struck={struck}", f"plain={[p[1] for p in plain]}"]
    if unit_wpg is not None:
        note_bits.append(f"won_per_g={unit_wpg:.2f}")

    return {
        "price": price,
        "list_price": list_price,
        "discount_rate": rate,
        "coupon_price": coupon_price,
        "wow_price": wow_price,
        "unit_price_won_per_g": unit_wpg,
        "note": " ".join(note_bits),
    }


def build_price_point(
    product_id: str,
    tokens: Iterable[dict[str, Any]],
    *,
    sold_out: bool = False,
    checked_at: Optional[datetime] = None,
    extra_note: str = "",
) -> PricePoint:
    """분류 결과를 PricePoint로 조립한다. 예외를 던지지 않는다."""
    checked_at = checked_at or datetime.now()
    try:
        f = classify_tokens(tokens)
    except Exception:  # 파서 버그로 앱이 죽으면 안 된다
        logger.exception("가격 토큰 분류 실패 product_id=%s", product_id)
        f = {
            "price": None, "list_price": None, "discount_rate": None,
            "coupon_price": None, "wow_price": None,
            "unit_price_won_per_g": None, "note": "classify_error",
        }

    if sold_out:
        availability = Availability.OUT_OF_STOCK
    elif f["price"] is not None:
        availability = Availability.IN_STOCK
    else:
        # 가격도 못 찾고 품절 표시도 없으면 판단 보류.
        # UNKNOWN이면 rules.py가 RESTOCK을 오발동시키지 않는다.
        availability = Availability.UNKNOWN

    note = f["note"]
    if f["unit_price_won_per_g"] is not None:
        note += f" wpg={f['unit_price_won_per_g']:.3f}"
    if extra_note:
        note += " | " + extra_note

    return PricePoint(
        product_id=product_id,
        checked_at=checked_at,
        price=f["price"],
        list_price=f["list_price"],
        discount_rate=f["discount_rate"],
        coupon_price=f["coupon_price"],
        wow_price=f["wow_price"],
        availability=availability,
        raw_note=note[:500],
    )


def clean_title(og_title: Optional[str]) -> str:
    """og:title에서 ' | 쿠팡' 꼬리를 떼어낸다."""
    if not og_title:
        return ""
    t = og_title.strip()
    for tail in (" | 쿠팡", "| 쿠팡", "- 쿠팡"):
        if t.endswith(tail):
            t = t[: -len(tail)].strip()
            break
    return t


def extract_ids(url: str) -> dict[str, Optional[str]]:
    """쿠팡 URL에서 product_id / itemId / vendorItemId 를 뽑는다."""
    pid = re.search(r"/vp/products/(\d+)", url or "")
    item = re.search(r"[?&]itemId=(\d+)", url or "")
    vendor = re.search(r"[?&]vendorItemId=(\d+)", url or "")
    return {
        "product_id": pid.group(1) if pid else None,
        "item_id": item.group(1) if item else None,
        "vendor_item_id": vendor.group(1) if vendor else None,
    }


def normalize_url(url: str) -> str:
    """추적 파라미터를 걷어내고 상품 식별에 필요한 것만 남긴다."""
    ids = extract_ids(url)
    if not ids["product_id"]:
        return url
    base = f"https://www.coupang.com/vp/products/{ids['product_id']}"
    params = []
    if ids["item_id"]:
        params.append(f"itemId={ids['item_id']}")
    if ids["vendor_item_id"]:
        params.append(f"vendorItemId={ids['vendor_item_id']}")
    return base + ("?" + "&".join(params) if params else "")


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    # 실제 쿠팡 페이지에서 채집한 토큰 (뉴욕웨이 골드 WPI, 2026-09-03)
    real = [
        {"t": "42%", "fs": 14.0, "lt": False},
        {"t": "45,900원", "fs": 22.0, "lt": False},
        {"t": "(10g당 153원)", "fs": 14.0, "lt": False},
        {"t": "79,900원", "fs": 14.0, "lt": True},
        {"t": "1,000원", "fs": 14.0, "lt": False},
        {"t": "할인", "fs": 14.0, "lt": False},
        {"t": "46,900원", "fs": 14.0, "lt": True},
    ]
    r = classify_tokens(real)
    assert r["price"] == 45900, r
    assert r["list_price"] == 79900, r
    assert r["discount_rate"] == 42, r
    assert r["coupon_price"] == 45900, r
    assert abs(r["unit_price_won_per_g"] - 15.3) < 0.01, r
    print("case1 실제 페이지     :", r)

    # 쿠폰 없는 단순 케이스
    simple = [
        {"t": "34%", "fs": 14.0, "lt": False},
        {"t": "26,230원", "fs": 22.0, "lt": False},
        {"t": "(10g 262원)", "fs": 14.0, "lt": False},
        {"t": "39,900원", "fs": 14.0, "lt": True},
    ]
    r2 = classify_tokens(simple)
    assert r2["price"] == 26230 and r2["list_price"] == 39900 and r2["coupon_price"] is None, r2
    print("case2 쿠폰없음        :", r2)

    # 할인 자체가 없는 정가 판매
    none_disc = [{"t": "31,000원", "fs": 22.0, "lt": False}]
    r3 = classify_tokens(none_disc)
    assert r3["price"] == 31000 and r3["list_price"] is None and r3["discount_rate"] is None, r3
    print("case3 할인없음        :", r3)

    # 할인율 뱃지가 없을 때 계산으로 채우는지
    calc = [
        {"t": "50,000원", "fs": 22.0, "lt": False},
        {"t": "100,000원", "fs": 14.0, "lt": True},
    ]
    r4 = classify_tokens(calc)
    assert r4["discount_rate"] == 50, r4
    print("case4 할인율 자동계산 :", r4)

    # 빈 입력 → price None (알림 안 나감)
    r5 = classify_tokens([])
    assert r5["price"] is None, r5
    pp = build_price_point("123", [])
    assert pp.price is None and pp.availability == Availability.UNKNOWN
    print("case5 빈입력          :", pp.availability, pp.price)

    # 품절
    pp2 = build_price_point("123", [], sold_out=True)
    assert pp2.availability == Availability.OUT_OF_STOCK
    print("case6 품절            :", pp2.availability)

    # 단위 단가 kg 환산
    assert abs(won_per_gram("(1kg당 20,000원)") - 20.0) < 0.001
    print("case7 kg환산          :", won_per_gram("(1kg당 20,000원)"))

    # URL 유틸
    u = "https://www.coupang.com/vp/products/9684343895?itemId=28960465352&vendorItemId=95890673988&sourceType=srp_product_ads&clickEventId=abc"
    ids = extract_ids(u)
    assert ids["product_id"] == "9684343895" and ids["vendor_item_id"] == "95890673988", ids
    assert normalize_url(u) == "https://www.coupang.com/vp/products/9684343895?itemId=28960465352&vendorItemId=95890673988"
    print("case8 URL 정규화      :", normalize_url(u))
    assert clean_title("뉴욕웨이 골드 WPI함유 단백질 보충제 곡물맛 | 쿠팡") == "뉴욕웨이 골드 WPI함유 단백질 보충제 곡물맛"
    print("case9 제목 정리       : OK")

    print("\nOK: parser.py self-test passed (9 cases)")
