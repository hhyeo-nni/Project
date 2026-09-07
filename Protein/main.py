"""프로틴 할인 알림 — 진입점.

실행:  프로젝트 폴더에서  ``python main.py``
       (또는 콘솔 창 없이 실행하려면 ``pythonw main.py`` / 실행.bat)

트레이에 상주하면서 설정한 주기마다 쿠팡 상품 가격을 확인하고,
조건에 걸리면 윈도우 토스트 알림을 띄운다.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

# 프로젝트 루트를 import 경로에 넣는다 (폴더명에 한글/공백이 있어 -m 실행이 어렵다)
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import load_config
from core.logging_setup import setup_logging
from core.notifier import Notifier, set_process_app_id
from core.paths import CONFIG_FILE, DB_FILE, ICON_FILE, ensure_dirs
from core.scheduler import Scheduler
from core.scraper import CoupangScraper
from core.store import Store

logger = logging.getLogger("main")

# 같은 프로그램이 두 번 뜨면 브라우저 프로필이 충돌한다 — 한 개만 뜨게 잠근다.
_LOCK_HANDLE = None


def _acquire_single_instance() -> bool:
    """이미 실행 중이면 False. 윈도우 뮤텍스를 쓴다."""
    global _LOCK_HANDLE
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        _LOCK_HANDLE = kernel32.CreateMutexW(None, wintypes.BOOL(True), "Global\\ProteinDealAlert_v1")
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        logger.debug("단일 인스턴스 검사 실패 — 그냥 진행", exc_info=True)
        return True


def main() -> int:
    ensure_dirs()
    setup_logging()

    # 작업표시줄이 이 프로그램을 'pythonw.exe'가 아니라 우리 앱으로 인식하게 한다.
    # 창을 하나라도 만들기 전에 불러야 효과가 있으므로 가장 먼저 호출한다.
    set_process_app_id()
    logger.info("=" * 60)
    logger.info("프로틴 할인 알림 시작")

    if not _acquire_single_instance():
        logger.warning("이미 실행 중입니다. 트레이 아이콘을 확인하세요.")
        try:
            Notifier(icon_path=ICON_FILE).notify_info(
                "프로틴 할인 알림", "이미 실행 중입니다. 우측 하단 트레이를 확인하세요."
            )
        except Exception:
            pass
        return 0

    cfg = load_config(CONFIG_FILE)
    store = Store(DB_FILE)
    notifier = Notifier(icon_path=ICON_FILE, sound=cfg.sound)

    def scraper_factory():
        # 사이클마다 새로 만들지만, 브라우저 프로필은 계속 재사용된다(신뢰 세션 유지).
        return CoupangScraper(headless=load_config(CONFIG_FILE).headless)

    sched = Scheduler(store=store, notifier=notifier, config=cfg, scraper_factory=scraper_factory)

    def on_settings_saved() -> None:
        """설정 창에서 저장하면 스케줄러가 새 설정을 바로 반영하게 한다."""
        try:
            new_cfg = load_config(CONFIG_FILE)
            sched._config = new_cfg          # 스케줄러는 매 사이클 이 값을 다시 읽는다
            notifier.sound = new_cfg.sound
            logger.info("설정 변경 반영: 주기=%d분 상품=%d개",
                        new_cfg.poll_interval_minutes, len(new_cfg.products))
        except Exception:
            logger.exception("설정 반영 실패")

    def open_settings() -> None:
        # 트레이 스레드에서 호출된다. settings_gui가 자체 스레드에서 Tk를 띄운다.
        from ui.settings_gui import open_settings as _open

        def probe(url: str) -> dict:
            """설정 창의 '상품 추가'에서 상품명을 자동으로 읽어온다(느리므로 GUI가 별도 스레드로 호출)."""
            with CoupangScraper(headless=load_config(CONFIG_FILE).headless) as sc:
                return sc.probe_dict(url)

        _open(store, config_path=CONFIG_FILE, on_saved=on_settings_saved, probe_fn=probe)

    from ui.tray import TrayApp

    def on_quit() -> None:
        logger.info("종료 처리 중…")
        try:
            sched.stop()
        except Exception:
            logger.exception("스케줄러 정지 실패")
        try:
            store.close()
        except Exception:
            logger.exception("DB 종료 실패")

    tray = TrayApp(sched, on_open_settings=open_settings, on_quit=on_quit, icon_path=ICON_FILE)

    sched.start()
    logger.info("감시 시작 — 상품 %d개, 주기 %d분", len(cfg.products), cfg.poll_interval_minutes)

    if not cfg.products:
        notifier.notify_info(
            "프로틴 할인 알림 실행됨",
            "감시할 상품이 없습니다. 트레이 아이콘 우클릭 → 설정에서 쿠팡 상품 URL을 추가하세요.",
        )
    else:
        notifier.notify_info(
            "프로틴 할인 알림 실행됨",
            f"상품 {len(cfg.products)}개를 {cfg.poll_interval_minutes}분마다 확인합니다.",
        )

    try:
        tray.run()   # 블로킹 — 트레이에서 '종료'를 누를 때까지
    except KeyboardInterrupt:
        logger.info("키보드 인터럽트로 종료")
        on_quit()
    logger.info("프로그램 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
