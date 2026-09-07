"""경로 상수 — 모든 파일 경로는 이 모듈을 통해서만 접근한다.

SPEC.md 2절 참고.
"""

from __future__ import annotations

from pathlib import Path

# 프로젝트 루트: 이 파일(core/paths.py)의 부모의 부모
ROOT: Path = Path(__file__).resolve().parent.parent

CONFIG_FILE: Path = ROOT / "config.json"
SELECTORS_FILE: Path = ROOT / "selectors.json"
DATA_DIR: Path = ROOT / "data"
DB_FILE: Path = DATA_DIR / "protein.db"
LOG_DIR: Path = DATA_DIR / "logs"
ICON_FILE: Path = ROOT / "ui" / "icon.png"
# 창 제목표시줄/작업표시줄용. .ico 는 16~256px 멀티 해상도라 작은 크기에서도 또렷하다.
ICON_ICO: Path = ROOT / "ui" / "icon.ico"


def ensure_dirs() -> None:
    """앱 동작에 필요한 디렉터리를 생성한다(없으면). 멱등."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "ui").mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    print("ROOT:", ROOT)
    print("CONFIG_FILE:", CONFIG_FILE)
    print("SELECTORS_FILE:", SELECTORS_FILE)
    print("DATA_DIR:", DATA_DIR)
    print("DB_FILE:", DB_FILE)
    print("LOG_DIR:", LOG_DIR)
    print("ICON_FILE:", ICON_FILE)

    assert ROOT.name == "프로틴 할인 알림", f"예상치 못한 루트: {ROOT}"
    assert CONFIG_FILE == ROOT / "config.json"
    assert DB_FILE == DATA_DIR / "protein.db"

    ensure_dirs()
    assert DATA_DIR.is_dir()
    assert LOG_DIR.is_dir()
    print("OK: paths.py self-test passed")
