"""쿠팡 연결 진단 도구.

## 언제 쓰나

가격이 계속 안 잡히거나(`None`), 상품 추가 시 상품명 조회가 안 될 때.
프로그램 전체를 켜지 않고 **수집 기능만 따로** 돌려서 어디가 막히는지 확인한다.

## 사용법

    python tools\\연결테스트.py                    # 기본 샘플 상품으로 진단
    python tools\\연결테스트.py <쿠팡 상품 URL>     # 특정 상품으로 진단

브라우저 창이 잠깐 떴다가 닫힙니다. 정상입니다.
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.config import load_config  # noqa: E402
from core.paths import CONFIG_FILE  # noqa: E402
from core.scraper import CoupangScraper  # noqa: E402

SAMPLE_URL = (
    "https://www.coupang.com/vp/products/9684343895"
    "?itemId=28960465352&vendorItemId=95890673988"
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="  %(asctime)s [%(levelname)s] %(message)s")

    url = sys.argv[1] if len(sys.argv) > 1 else SAMPLE_URL
    is_sample = url == SAMPLE_URL

    print("=" * 66)
    print("  쿠팡 연결 진단")
    print("=" * 66)
    print(f"  대상: {url[:90]}")
    if is_sample:
        print("  (기본 샘플 상품입니다. 특정 상품을 보려면 URL을 인자로 넘기세요)")
    print()
    print("  브라우저 창이 뜹니다. 손대지 말고 기다려 주세요.")
    print("  홈 → 검색 → 상품 순서로 이동하며, 1~2분 정도 걸립니다.")
    print()

    try:
        cfg = load_config(CONFIG_FILE)
        hide = cfg.headless
    except Exception:
        hide = False

    try:
        with CoupangScraper(headless=hide) as sc:
            print("\n─ 1단계: 상품명 조회(probe) ─────────────────────────")
            meta = sc.probe(url)
            if meta.name:
                print(f"  ✅ 상품명: {meta.name}")
            else:
                print("  ❌ 상품명을 못 읽었습니다.")
            print(f"     productId={meta.product_id or '?'} vendorItemId={meta.vendor_item_id or '?'}")

            print("\n─ 2단계: 가격 수집(fetch) ───────────────────────────")
            pp = sc.fetch(url)
            if pp.price is not None:
                print(f"  ✅ 현재가   : {pp.price:,}원")
                if pp.list_price:
                    print(f"     정가     : {pp.list_price:,}원")
                if pp.discount_rate is not None:
                    print(f"     할인율   : {pp.discount_rate}%")
                if pp.coupon_price:
                    print(f"     쿠폰적용 : {pp.coupon_price:,}원")
                print(f"     재고     : {pp.availability.value}")
                print("\n  ★ 정상입니다. 프로그램이 제대로 동작할 상태입니다.")
                return 0

            print("  ❌ 가격을 못 읽었습니다.")
            print(f"     진단 메모: {pp.raw_note}")
            print()
            print("  ── 원인별 대처법 ──")
            note = pp.raw_note or ""
            if "access_denied" in note:
                print("   · Akamai 차단입니다. 30분~2시간 뒤 다시 시도해 보세요.")
                print("   · 설정에서 확인 주기를 30분 이상으로 유지하세요.")
                print("   · 공유기를 재시작해 공인 IP를 바꾸면 즉시 풀리기도 합니다.")
            elif "no_price_container" in note:
                print("   · 페이지는 열렸는데 가격 영역을 못 찾았습니다.")
                print("   · 품절 상품이거나, 쿠팡이 화면을 개편한 경우입니다.")
                print("   · selectors.json 의 price_container 값을 확인해 보세요.")
            elif "page_not_ready" in note:
                print("   · 페이지 로딩이 끝나지 않았습니다. 네트워크가 느리거나 일시 차단입니다.")
                print("   · 잠시 후 다시 시도해 보세요.")
            else:
                print("   · data\\logs\\app.log 를 열어 자세한 오류를 확인하세요.")
            return 1
    except Exception as e:
        print(f"\n  오류 발생: {type(e).__name__}: {e}")
        print("  data\\logs\\app.log 를 확인하세요.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
