"""설정 파일(config.json) 로딩/저장 (SPEC.md 6절)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.models import AlertRule, Product, RuleKind
from core.paths import CONFIG_FILE

logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    poll_interval_minutes: int = 30
    jitter_minutes: int = 5
    cooldown_hours: int = 12
    renotify_on_lower: bool = True
    sound: bool = False
    request_delay_seconds: tuple[int, int] = (5, 15)
    headless: bool = True   # 확인할 때 브라우저 창을 화면 밖에서 띄움(진짜 헤드리스 아님)
    products: list[dict] = field(default_factory=list)   # 원본 dict 유지
    # 토스트 알림의 AUMID/표시 이름. None(기본값)이면 core.notifier.DEFAULT_APP_ID
    # ("프로틴 할인 알림")를 쓴다. 커스텀 AUMID 등록에도 토스트가 안 뜨는 환경이면
    # 이미 시스템에 등록되어 있는 다른 AUMID(예: core.notifier.POWERSHELL_APP_ID)를
    # 여기에 넣어 빌려 쓸 수 있다 — 진단 도구: tools/알림테스트.py 참고.
    notify_app_id: Optional[str] = None
    # 설정 창 UI 테마. "light" 또는 "dark" (sv-ttk).
    theme: str = "light"
    # 설정 창 UI 글꼴.
    font_family: str = "Noto Sans KR"
    font_size: int = 10


def _default_config_dict() -> dict[str, Any]:
    return {
        "poll_interval_minutes": 30,
        "jitter_minutes": 5,
        "cooldown_hours": 12,
        "renotify_on_lower": True,
        "sound": False,
        "request_delay_seconds": [5, 15],
        "headless": True,
        "products": [],
        "notify_app_id": None,
        "theme": "light",
        "font_family": "Noto Sans KR",
        "font_size": 10,
    }


def _config_from_dict(data: dict[str, Any]) -> AppConfig:
    defaults = _default_config_dict()
    request_delay = data.get("request_delay_seconds", defaults["request_delay_seconds"])
    return AppConfig(
        poll_interval_minutes=data.get("poll_interval_minutes", defaults["poll_interval_minutes"]),
        jitter_minutes=data.get("jitter_minutes", defaults["jitter_minutes"]),
        cooldown_hours=data.get("cooldown_hours", defaults["cooldown_hours"]),
        renotify_on_lower=data.get("renotify_on_lower", defaults["renotify_on_lower"]),
        sound=data.get("sound", defaults["sound"]),
        request_delay_seconds=tuple(request_delay),  # type: ignore[assignment]
        headless=data.get("headless", defaults["headless"]),
        products=data.get("products", []),
        notify_app_id=data.get("notify_app_id", defaults["notify_app_id"]),
        theme=data.get("theme", defaults["theme"]),
        font_family=data.get("font_family", defaults["font_family"]),
        font_size=data.get("font_size", defaults["font_size"]),
    )


def load_config(path: Path = CONFIG_FILE) -> AppConfig:
    """설정 파일을 로드한다. 없으면 기본값으로 파일을 생성한 뒤 반환한다.

    JSON 파싱 실패 시에는 로그를 남기고 기본값을 반환한다(앱이 죽으면 안 됨).
    """
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg, path)
        return cfg

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("config.json 파싱 실패, 기본값 사용: %s", exc)
        return AppConfig()

    try:
        return _config_from_dict(data)
    except Exception as exc:  # 방어적: 어떤 형식 오류든 앱이 죽지 않도록
        logger.error("config.json 형식 오류, 기본값 사용: %s", exc)
        return AppConfig()


def save_config(cfg: AppConfig, path: Path = CONFIG_FILE) -> None:
    """설정을 임시파일에 쓰고 os.replace로 원자적 교체한다."""
    path.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(cfg)
    # request_delay_seconds는 tuple -> JSON에서는 list로 직렬화
    data["request_delay_seconds"] = list(cfg.request_delay_seconds)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(tmp_path, path)


def rules_of(product_entry: dict) -> list[AlertRule]:
    """product dict의 rules 목록을 AlertRule 리스트로 변환한다.

    알 수 없는 kind 문자열은 로그를 남기고 건너뛴다.
    """
    result: list[AlertRule] = []
    for rule_dict in product_entry.get("rules", []):
        kind_str = rule_dict.get("kind")
        try:
            kind = RuleKind(kind_str)
        except ValueError:
            logger.warning("알 수 없는 규칙 kind 무시: %s", kind_str)
            continue
        result.append(
            AlertRule(
                kind=kind,
                threshold=rule_dict.get("threshold", 0),
                enabled=rule_dict.get("enabled", True),
            )
        )
    return result


def product_of(product_entry: dict) -> Product:
    """product dict를 Product 객체로 변환한다."""
    return Product(
        product_id=product_entry["product_id"],
        url=product_entry["url"],
        name=product_entry["name"],
        item_id=product_entry.get("item_id"),
        vendor_item_id=product_entry.get("vendor_item_id"),
        protein_grams=product_entry.get("protein_grams"),
        enabled=product_entry.get("enabled", True),
        added_at=None,
        image_url=product_entry.get("image_url"),
    )


if __name__ == "__main__":
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="protein_config_test_"))
    try:
        test_path = tmp_dir / "config.json"

        # 1) 파일 없을 때 로드 -> 기본값 + 파일 생성 확인
        assert not test_path.exists()
        cfg = load_config(test_path)
        assert test_path.exists()
        assert cfg.poll_interval_minutes == 30
        assert cfg.products == []
        assert cfg.request_delay_seconds == (5, 15)
        assert cfg.theme == "light"
        assert cfg.font_family == "맑은 고딕"
        assert cfg.font_size == 10

        # 2) 수정 후 저장 -> 재로드 일치 확인
        cfg.poll_interval_minutes = 60
        cfg.products.append(
            {
                "product_id": "1234567890",
                "url": "https://www.coupang.com/vp/products/1234567890?itemId=1&vendorItemId=2",
                "name": "마이프로틴 임팩트 웨이 1kg",
                "protein_grams": 800.0,
                "enabled": True,
                "rules": [
                    {"kind": "target_price", "threshold": 45000, "enabled": True},
                    {"kind": "lowest_ever", "threshold": 0, "enabled": True},
                    {"kind": "unknown_kind_test", "threshold": 1, "enabled": True},
                ],
            }
        )
        save_config(cfg, test_path)

        cfg.theme = "dark"
        cfg.font_size = 12
        save_config(cfg, test_path)

        cfg2 = load_config(test_path)
        assert cfg2.poll_interval_minutes == 60
        assert len(cfg2.products) == 1
        assert cfg2.theme == "dark"
        assert cfg2.font_size == 12

        # 3) rules_of / product_of 변환 확인 (알 수 없는 kind는 스킵)
        entry = cfg2.products[0]
        rules = rules_of(entry)
        assert len(rules) == 2, f"알 수 없는 kind는 건너뛰어야 함, got {len(rules)}"
        assert rules[0].kind == RuleKind.TARGET_PRICE
        assert rules[0].threshold == 45000
        assert rules[1].kind == RuleKind.LOWEST_EVER

        product = product_of(entry)
        assert product.product_id == "1234567890"
        assert product.protein_grams == 800.0

        # 4) JSON 파싱 실패 시 기본값 반환 (앱이 죽지 않음)
        broken_path = tmp_dir / "broken_config.json"
        with open(broken_path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ,,, ")
        cfg3 = load_config(broken_path)
        assert cfg3.poll_interval_minutes == 30  # 기본값

        print("OK: config.py self-test passed")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
