"""SQLite 저장소 (SPEC.md 3절).

스레드 안전: sqlite3.connect(check_same_thread=False) + threading.Lock 으로
모든 쿼리를 보호한다. WAL 모드 활성화.

**alert_history 스키마 참고**: SPEC.md가 명시한 alert_history 테이블은
(id, product_id, kind, fired_at, price, message) 6개 컬럼만 가진다.
AlertEvent 데이터클래스의 product_name/prev_price/url 필드는 이 테이블에
저장되지 않는다. last_alert()가 AlertEvent를 재구성할 때 product_name과
url은 products 테이블을 조회해 채우고(없으면 빈 문자열), prev_price는
저장된 값이 없으므로 None으로 채운다. rules.py의 쿨다운 판정은
fired_at/price/kind만 사용하므로 이 부분은 영향이 없다.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.models import AlertEvent, Availability, PricePoint, Product, RuleKind
from core.paths import DB_FILE

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, db_path: Path = DB_FILE) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._create_schema()

    def _create_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                url TEXT,
                name TEXT,
                item_id TEXT,
                vendor_item_id TEXT,
                protein_grams REAL,
                enabled INTEGER,
                added_at TEXT,
                image_url TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT,
                checked_at TEXT,
                price INTEGER,
                list_price INTEGER,
                discount_rate INTEGER,
                coupon_price INTEGER,
                wow_price INTEGER,
                availability TEXT,
                raw_note TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_history_product_checked "
            "ON price_history(product_id, checked_at)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT,
                kind TEXT,
                fired_at TEXT,
                price INTEGER,
                message TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alert_history_product_kind_fired "
            "ON alert_history(product_id, kind, fired_at)"
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- products ----------------

    def upsert_product(self, p: Product) -> None:
        added_at = p.added_at.isoformat() if p.added_at is not None else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO products
                    (product_id, url, name, item_id, vendor_item_id,
                     protein_grams, enabled, added_at, image_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                    url=excluded.url,
                    name=excluded.name,
                    item_id=excluded.item_id,
                    vendor_item_id=excluded.vendor_item_id,
                    protein_grams=excluded.protein_grams,
                    enabled=excluded.enabled,
                    added_at=excluded.added_at,
                    image_url=excluded.image_url
                """,
                (
                    p.product_id,
                    p.url,
                    p.name,
                    p.item_id,
                    p.vendor_item_id,
                    p.protein_grams,
                    1 if p.enabled else 0,
                    added_at,
                    p.image_url,
                ),
            )
            self._conn.commit()

    def get_product(self, product_id: str) -> Optional[Product]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM products WHERE product_id = ?", (product_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_product(row)

    def list_products(self, enabled_only: bool = False) -> list[Product]:
        query = "SELECT * FROM products"
        params: tuple = ()
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY product_id"
        with self._lock:
            cur = self._conn.execute(query, params)
            rows = cur.fetchall()
        return [self._row_to_product(row) for row in rows]

    def delete_product(self, product_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM products WHERE product_id = ?", (product_id,)
            )
            self._conn.commit()

    @staticmethod
    def _row_to_product(row: sqlite3.Row) -> Product:
        added_at_str = row["added_at"]
        return Product(
            product_id=row["product_id"],
            url=row["url"],
            name=row["name"],
            item_id=row["item_id"],
            vendor_item_id=row["vendor_item_id"],
            protein_grams=row["protein_grams"],
            enabled=bool(row["enabled"]),
            added_at=datetime.fromisoformat(added_at_str) if added_at_str else None,
            image_url=row["image_url"],
        )

    # ---------------- price history ----------------

    def add_price(self, pp: PricePoint) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO price_history
                    (product_id, checked_at, price, list_price, discount_rate,
                     coupon_price, wow_price, availability, raw_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pp.product_id,
                    pp.checked_at.isoformat(),
                    pp.price,
                    pp.list_price,
                    pp.discount_rate,
                    pp.coupon_price,
                    pp.wow_price,
                    pp.availability.value,
                    pp.raw_note,
                ),
            )
            self._conn.commit()

    def latest_price(self, product_id: str) -> Optional[PricePoint]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM price_history
                WHERE product_id = ?
                ORDER BY checked_at DESC, id DESC
                LIMIT 1
                """,
                (product_id,),
            )
            row = cur.fetchone()
        return self._row_to_price_point(row) if row is not None else None

    def previous_price(self, product_id: str) -> Optional[PricePoint]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM price_history
                WHERE product_id = ?
                ORDER BY checked_at DESC, id DESC
                LIMIT 1 OFFSET 1
                """,
                (product_id,),
            )
            row = cur.fetchone()
        return self._row_to_price_point(row) if row is not None else None

    def min_price(self, product_id: str, days: Optional[int] = None) -> Optional[int]:
        with self._lock:
            if days is None:
                cur = self._conn.execute(
                    """
                    SELECT MIN(price) AS m FROM price_history
                    WHERE product_id = ? AND price IS NOT NULL
                    """,
                    (product_id,),
                )
            else:
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                cur = self._conn.execute(
                    """
                    SELECT MIN(price) AS m FROM price_history
                    WHERE product_id = ? AND price IS NOT NULL AND checked_at >= ?
                    """,
                    (product_id, cutoff),
                )
            row = cur.fetchone()
        return row["m"] if row is not None and row["m"] is not None else None

    def avg_discount_rate(self, product_id: str, days: int = 7) -> Optional[float]:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT AVG(discount_rate) AS a FROM price_history
                WHERE product_id = ? AND discount_rate IS NOT NULL AND checked_at >= ?
                """,
                (product_id, cutoff),
            )
            row = cur.fetchone()
        return row["a"] if row is not None and row["a"] is not None else None

    def price_history(self, product_id: str, days: int = 90) -> list[PricePoint]:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM price_history
                WHERE product_id = ? AND checked_at >= ?
                ORDER BY checked_at ASC, id ASC
                """,
                (product_id, cutoff),
            )
            rows = cur.fetchall()
        return [self._row_to_price_point(row) for row in rows]

    @staticmethod
    def _row_to_price_point(row: sqlite3.Row) -> PricePoint:
        return PricePoint(
            product_id=row["product_id"],
            checked_at=datetime.fromisoformat(row["checked_at"]),
            price=row["price"],
            list_price=row["list_price"],
            discount_rate=row["discount_rate"],
            coupon_price=row["coupon_price"],
            wow_price=row["wow_price"],
            availability=Availability(row["availability"]),
            raw_note=row["raw_note"] or "",
        )

    # ---------------- alert history ----------------

    def add_alert(self, ev: AlertEvent) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO alert_history
                    (product_id, kind, fired_at, price, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ev.product_id,
                    ev.kind.value,
                    ev.fired_at.isoformat(),
                    ev.price,
                    ev.message,
                ),
            )
            self._conn.commit()

    def last_alert(self, product_id: str, kind: RuleKind) -> Optional[AlertEvent]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM alert_history
                WHERE product_id = ? AND kind = ?
                ORDER BY fired_at DESC, id DESC
                LIMIT 1
                """,
                (product_id, kind.value),
            )
            row = cur.fetchone()
        if row is None:
            return None

        product = self.get_product(product_id)
        product_name = product.name if product is not None else ""
        url = product.url if product is not None else ""

        return AlertEvent(
            product_id=row["product_id"],
            product_name=product_name,
            kind=RuleKind(row["kind"]),
            fired_at=datetime.fromisoformat(row["fired_at"]),
            price=row["price"],
            prev_price=None,  # 스키마에 저장되지 않음 (SPEC.md 3절 alert_history 컬럼 참고)
            message=row["message"] or "",
            url=url,
        )


if __name__ == "__main__":
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="protein_store_test_"))
    try:
        db_path = tmp_dir / "test.db"
        store = Store(db_path)

        # products
        p = Product(
            product_id="p1",
            url="https://www.coupang.com/vp/products/p1?itemId=1&vendorItemId=2",
            name="테스트 프로틴",
            protein_grams=800.0,
            added_at=datetime.now(),
        )
        store.upsert_product(p)
        # 같은 product_id로 두 번 upsert -> 행 1개 유지 확인
        p.name = "테스트 프로틴 (수정됨)"
        store.upsert_product(p)

        products = store.list_products()
        assert len(products) == 1, f"행이 1개여야 함, got {len(products)}"
        assert products[0].name == "테스트 프로틴 (수정됨)"

        fetched = store.get_product("p1")
        assert fetched is not None
        assert fetched.product_id == "p1"

        # price history: 5개 시계열 삽입
        now = datetime.now()
        prices = [50000, 48000, 45000, 47000, 43000]
        for i, price in enumerate(prices):
            pp = PricePoint(
                product_id="p1",
                checked_at=now + timedelta(minutes=i),
                price=price,
                list_price=60000,
                discount_rate=10 + i,
                coupon_price=None,
                wow_price=None,
                availability=Availability.IN_STOCK,
                raw_note=f"test-{i}",
            )
            store.add_price(pp)

        latest = store.latest_price("p1")
        assert latest is not None
        assert latest.price == 43000, f"latest_price는 43000이어야 함, got {latest.price}"

        prev = store.previous_price("p1")
        assert prev is not None
        assert prev.price == 47000, f"previous_price는 47000이어야 함, got {prev.price}"

        mn = store.min_price("p1")
        assert mn == 43000, f"min_price는 43000이어야 함, got {mn}"

        mn_days = store.min_price("p1", days=30)
        assert mn_days == 43000, f"min_price(days=30)는 43000이어야 함, got {mn_days}"

        mn_days_none = store.min_price("p1", days=0)
        # days=0 -> cutoff는 거의 now, 방금 넣은 데이터도 checked_at >= cutoff 조건 만족할 수 있음
        # (특정 값 검증보다는 예외 없이 동작하는지만 확인)

        avg_disc = store.avg_discount_rate("p1", days=7)
        assert avg_disc is not None
        assert abs(avg_disc - sum(range(10, 15)) / 5) < 1e-6

        hist = store.price_history("p1", days=90)
        assert len(hist) == 5

        # alert history
        ev = AlertEvent(
            product_id="p1",
            product_name="테스트 프로틴",
            kind=RuleKind.TARGET_PRICE,
            fired_at=now,
            price=43000,
            prev_price=47000,
            message="43,000원 · 목표가 도달",
            url=p.url,
        )
        store.add_alert(ev)

        last = store.last_alert("p1", RuleKind.TARGET_PRICE)
        assert last is not None
        assert last.price == 43000
        assert last.kind == RuleKind.TARGET_PRICE
        assert last.product_name == "테스트 프로틴 (수정됨)"  # products 테이블 최신값 반영
        assert last.prev_price is None  # 스키마상 저장 안 됨

        no_alert = store.last_alert("p1", RuleKind.RESTOCK)
        assert no_alert is None

        # delete_product
        store.delete_product("p1")
        assert store.get_product("p1") is None

        store.close()
        print("OK: store.py self-test passed")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
