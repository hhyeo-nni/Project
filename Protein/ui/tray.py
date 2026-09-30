"""윈도우 트레이 아이콘 — 프로그램의 상주 얼굴.

우클릭 메뉴에서 즉시 확인 / 일시정지 / 설정 / 로그 열기 / 종료를 할 수 있다.
pystray는 자체 이벤트 루프를 돌리므로 ``run()`` 은 블로킹이다 — main.py가
메인 스레드에서 마지막에 호출한다.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime
from typing import Callable, Optional

from core.paths import ICON_FILE, LOG_DIR

logger = logging.getLogger(__name__)


def _fmt_time(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M") if dt else "-"


class TrayApp:
    """스케줄러를 감싸는 트레이 UI.

    ## 아이콘 자가 복구

    PC가 절전에 들어갔다 깨어나면 트레이 아이콘이 사라지는 일이 있다. pystray는
    탐색기 재시작 시 오는 ``WM_TASKBARCREATED`` 를 처리하지만, 프로세스가 절전으로
    멈춰 있는 동안 그 브로드캐스트가 지나가 버리면 받지 못한다. 그러면 프로그램은
    멀쩡히 돌고 있는데 아이콘만 영영 사라져서, 사용자는 "실행이 안 된다"고 느끼고
    다시 실행해봐야 중복 실행 방지에 걸려 "이미 실행 중"이라는 안내만 받게 된다
    (2026-09-15에 실제로 발생).

    그래서 메뉴 갱신 스레드가 약 10초마다 아이콘이 살아있는지 확인하고, 사라졌으면
    스스로 다시 등록한다.
    """

    def __init__(
        self,
        scheduler,
        on_open_settings: Callable[[], None],
        on_quit: Optional[Callable[[], None]] = None,
        icon_path=None,
    ) -> None:
        self.scheduler = scheduler
        self.on_open_settings = on_open_settings
        self.on_quit = on_quit
        self.icon_path = icon_path or ICON_FILE
        self.icon = None
        # 메뉴 자동 갱신용 (아래 _menu_refresher 주석 참고)
        self._stop_refresh = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None
        self._last_menu_text = ""
        self._heal_count = 0   # 아이콘을 다시 등록한 횟수(진단용)

    # ── 메뉴 항목 ───────────────────────────────────────────────────────
    def _status_text(self, _item=None) -> str:
        if not self.scheduler.is_running:
            return "상태: 정지됨"
        if self.scheduler.is_paused:
            return "상태: 일시정지"
        return f"다음 확인 {_fmt_time(self.scheduler.next_run_at)} (최근 {_fmt_time(self.scheduler.last_run_at)})"

    def _pause_text(self, _item=None) -> str:
        return "▶ 감시 재개" if self.scheduler.is_paused else "⏸ 일시정지"

    def _do_check_now(self, _icon=None, _item=None) -> None:
        try:
            logger.info("트레이: 지금 확인 요청")
            self.scheduler.run_once_async()
        except Exception:
            logger.exception("지금 확인 실패")

    def _do_toggle_pause(self, _icon=None, _item=None) -> None:
        try:
            if self.scheduler.is_paused:
                self.scheduler.resume()
                logger.info("트레이: 감시 재개")
            else:
                self.scheduler.pause()
                logger.info("트레이: 일시정지")
        except Exception:
            logger.exception("일시정지 토글 실패")

    def _do_settings(self, _icon=None, _item=None) -> None:
        try:
            logger.info("트레이: 설정 창 열기")
            self.on_open_settings()
        except Exception:
            logger.exception("설정 창 열기 실패")

    def _do_open_logs(self, _icon=None, _item=None) -> None:
        try:
            os.startfile(str(LOG_DIR))  # type: ignore[attr-defined]
        except Exception:
            logger.exception("로그 폴더 열기 실패")

    def _do_quit(self, _icon=None, _item=None) -> None:
        logger.info("트레이: 종료 요청")
        self._stop_refresh.set()
        try:
            if self.on_quit:
                self.on_quit()
        except Exception:
            logger.exception("종료 처리 중 오류")
        finally:
            try:
                if self.icon is not None:
                    self.icon.stop()
            except Exception:
                logger.debug("아이콘 정지 실패", exc_info=True)

    # ── 실행 ────────────────────────────────────────────────────────────
    def _load_image(self):
        from PIL import Image, ImageDraw
        try:
            return Image.open(str(self.icon_path))
        except Exception:
            logger.warning("아이콘 파일을 못 읽어 임시 아이콘을 그립니다: %s", self.icon_path)
            img = Image.new("RGBA", (64, 64), (30, 34, 45, 255))
            dr = ImageDraw.Draw(img)
            dr.ellipse((8, 8, 56, 56), fill=(46, 204, 113, 255))
            return img

    def build(self):
        import pystray
        from pystray import MenuItem as Item

        menu = pystray.Menu(
            Item(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            Item("🔍 지금 확인", self._do_check_now),
            Item(self._pause_text, self._do_toggle_pause),
            pystray.Menu.SEPARATOR,
            # default=True 인 항목이 아이콘을 클릭했을 때 실행된다.
            # (윈도우에서는 왼쪽 버튼을 떼는 순간 실행되므로 한 번 클릭·더블클릭 모두 동작한다.
            #  설정 창은 싱글턴이라 두 번 눌려도 창이 하나만 뜨고 앞으로 나온다)
            Item("⚙ 설정 / 상품 관리", self._do_settings, default=True),
            Item("📂 로그 폴더 열기", self._do_open_logs),
            pystray.Menu.SEPARATOR,
            Item("종료", self._do_quit),
        )
        self.icon = pystray.Icon(
            "protein_deal_alert",
            self._load_image(),
            "프로틴 할인 알림",
            menu,
        )
        return self.icon

    # ── 트레이 아이콘 자가 복구 ──────────────────────────────────────────
    def _icon_is_registered(self) -> bool:
        """트레이에 우리 아이콘이 아직 등록돼 있는가.

        ``Shell_NotifyIcon(NIM_MODIFY)`` 는 해당 아이콘이 없으면 **예외 없이 False**를
        돌려준다(pystray는 이 함수에 errcheck를 걸지 않는다). 그래서 이 반환값이
        아이콘 생존 여부를 확인하는 가장 가벼운 방법이다.

        판단이 불가능한 상황(아직 준비 전, 내부 구조 변경 등)에서는 True를 돌려
        **괜히 건드리지 않는다.** 멀쩡한 아이콘을 다시 등록하면 중복으로 생길 수 있다.
        """
        icon = self.icon
        if icon is None or not getattr(icon, "visible", False):
            return True                       # 아직 안 떴거나 종료 중 — 판단 보류
        hwnd = getattr(icon, "_hwnd", None)
        if not hwnd:
            return True
        try:
            import ctypes

            from pystray._util import win32

            data = win32.NOTIFYICONDATAW(
                cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW),
                hWnd=hwnd,
                hID=id(icon),                 # pystray의 _message 와 동일한 식별자
                uFlags=win32.NIF_TIP,
                szTip=icon.title or "",
            )
            return bool(win32.Shell_NotifyIcon(win32.NIM_MODIFY, data))
        except Exception:
            logger.debug("트레이 아이콘 상태 확인 실패(무시)", exc_info=True)
            return True

    def _heal_icon(self) -> bool:
        """사라진 트레이 아이콘을 다시 등록한다. 성공하면 True."""
        icon = self.icon
        if icon is None:
            return False
        try:
            icon._show()              # Shell_NotifyIcon(NIM_ADD)
            try:
                icon._update_menu()   # 메뉴 핸들도 새로 만들어 둔다
            except Exception:
                logger.debug("복구 후 메뉴 재생성 실패(무시)", exc_info=True)
            self._heal_count += 1
            logger.info("트레이 아이콘이 사라져 다시 등록했습니다 (누적 %d회).", self._heal_count)
            return True
        except Exception:
            logger.debug("트레이 아이콘 재등록 실패(무시)", exc_info=True)
            return False

    def _menu_refresher(self) -> None:
        """메뉴에 표시되는 시각을 주기적으로 갱신한다.

        pystray의 윈도우 백엔드는 **메뉴를 시작할 때 한 번만 만들어 두고 재사용**한다.
        그래서 "다음 확인 HH:MM" 같이 콜러블로 만든 문구는 처음 만들어진 값에 굳어버린다.
        프로그램이 막 뜬 시점에는 아직 다음 점검 시각이 정해지지 않아 "-" 로 보이고,
        그 뒤로 아무리 시간이 지나도 계속 "-" 였다(메뉴 항목을 한 번 클릭하면 그때서야
        메뉴가 다시 만들어져 시간이 나타났다 — 사용자가 겪은 증상이 정확히 이것이다).

        pystray 문서도 "메뉴 클릭 외의 이유로 값이 바뀌면 update_menu()를 불러야 한다"고
        안내한다. 그래서 표시될 문구가 실제로 달라졌을 때만 메뉴를 다시 만든다
        (매번 다시 만들면 사용자가 메뉴를 열어둔 순간과 겹칠 수 있어 최소한으로 줄인다).
        """
        ticks = 0
        while not self._stop_refresh.is_set():
            try:
                text = self._status_text() + "|" + self._pause_text()
                if text != self._last_menu_text and self.icon is not None:
                    self._last_menu_text = text
                    self.icon.update_menu()
            except Exception:
                logger.debug("트레이 메뉴 갱신 실패(무시)", exc_info=True)

            # 아이콘 생존 확인은 매번 할 필요가 없어 2회에 한 번(약 10초)만 한다.
            ticks += 1
            if ticks % 2 == 0:
                try:
                    if not self._icon_is_registered():
                        self._heal_icon()
                except Exception:
                    logger.debug("트레이 아이콘 자가 복구 중 오류(무시)", exc_info=True)

            self._stop_refresh.wait(5.0)

    def run(self) -> None:
        """블로킹. 메인 스레드에서 호출할 것."""
        if self.icon is None:
            self.build()
        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(
            target=self._menu_refresher, name="TrayMenuRefresher", daemon=True
        )
        self._refresh_thread.start()
        self.icon.run()


if __name__ == "__main__":
    import io, time, threading
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    class FakeScheduler:
        is_running = True
        _paused = False
        next_run_at = datetime.now()
        last_run_at = datetime.now()

        @property
        def is_paused(self):
            return self._paused

        def pause(self):
            self._paused = True
            print("  → 일시정지됨")

        def resume(self):
            self._paused = False
            print("  → 재개됨")

        def run_once_async(self):
            print("  → 지금 확인 실행")

    app = TrayApp(FakeScheduler(), on_open_settings=lambda: print("  → 설정 창 열기"))
    print("트레이 아이콘을 띄웁니다. 우측 하단 트레이에서 확인하세요. 12초 뒤 자동 종료됩니다.")
    threading.Timer(12.0, lambda: app.icon and app.icon.stop()).start()
    app.run()
    print("트레이 종료 완료")
