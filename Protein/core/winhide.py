"""수집용 브라우저 창을 작업표시줄에서 없애는 유틸 (Windows 전용).

## 왜 필요한가

수집할 때 브라우저를 화면 밖(``-32000,-32000``)에 띄워도 **작업표시줄에는 버튼이 남는다.**
게다가 SeleniumBase UC 모드는 페이지를 열 때마다 자동화 연결을 끊었다 다시 붙는데
(그게 Akamai를 뚫는 원리다), 그때마다 창이 다시 표시되면서 작업표시줄 버튼이 깜빡인다.
사용자 입장에서는 30분마다 뭔가가 번쩍거리는 셈이라 거슬린다.

## 어떻게 없애는가 (2026-09-04 실측)

======================================  ==================================  ===============
방법                                     지속성                               수집 정상?
======================================  ==================================  ===============
``WS_EX_TOOLWINDOW`` 확장 스타일 부여     ✅ 한 번 적용하면 계속 유지          ✅ 정상
``ShowWindow(SW_HIDE)`` 완전 숨김        ❌ 페이지 이동마다 다시 보임          ✅ 정상
======================================  ==================================  ===============

그래서 ``WS_EX_TOOLWINDOW`` 를 쓴다. 이 스타일이 붙은 창은 작업표시줄에 나타나지 않고,
UC 모드가 창을 다시 표시해도 스타일은 유지된다(3회 이동 연속 검증).

창은 브라우저 기동 후 약 4초 뒤에 생기므로, **기동 전부터 감시 스레드를 돌려**
창이 생기는 즉시(폴링 간격 50ms 이내) 스타일을 입힌다. 실측상 작업표시줄에
버튼이 보이는 시간은 최대 50ms 남짓이라 사람 눈에는 잡히지 않는다.

실패해도 절대 예외를 밖으로 던지지 않는다. 창 숨기기가 안 되는 것보다
수집이 멈추는 게 훨씬 나쁘기 때문이다.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

CHROME_WINDOW_CLASS = "Chrome_WidgetWin_1"

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_SW_HIDE = 0
_SW_SHOWNA = 8  # 활성화하지 않고 표시 — 포커스를 뺏지 않는다

# 창이 생기기 전에는 촘촘히, 잡은 뒤에는 느슨하게 감시한다.
_POLL_FAST_SEC = 0.05
_POLL_SLOW_SEC = 1.0
_FAST_PHASE_SEC = 30.0


def _win32():
    """user32 핸들과 EnumWindows 콜백 타입을 돌려준다. Windows가 아니면 None."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.restype = ctypes.c_long
        proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        return ctypes, user32, proc
    except Exception:
        logger.debug("Windows API를 쓸 수 없는 환경 — 작업표시줄 숨기기 건너뜀", exc_info=True)
        return None


class TaskbarHider:
    """새로 뜨는 크롬 창을 감시해 작업표시줄에서 감춘다.

    사용법::

        hider = TaskbarHider()
        hider.start()          # 브라우저를 띄우기 **직전에** 시작할 것
        driver = Driver(...)   # 이 사이에 창이 생기면 즉시 잡아서 숨긴다
        ...
        hider.stop()
    """

    def __init__(self, window_class: str = CHROME_WINDOW_CLASS) -> None:
        self.window_class = window_class
        self._known: set[int] = set()      # 시작 시점에 이미 있던 창(= 사용자 것, 건드리지 않음)
        self._handled: set[int] = set()    # 우리가 숨긴 창
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._api = _win32()

    # ── 내부 ────────────────────────────────────────────────────────────
    def _enum_windows(self) -> list[int]:
        if not self._api:
            return []
        ctypes, user32, proc = self._api
        found: list[int] = []

        def cb(hwnd, _lparam):
            try:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value == self.window_class:
                    found.append(hwnd)
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(proc(cb), 0)
        except Exception:
            logger.debug("창 목록 조회 실패", exc_info=True)
        return found

    def _is_hidden_from_taskbar(self, hwnd: int) -> bool:
        if not self._api:
            return True
        _ctypes, user32, _proc = self._api
        try:
            ex = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & 0xFFFFFFFF
            return bool(ex & _WS_EX_TOOLWINDOW)
        except Exception:
            return True

    def _hide_from_taskbar(self, hwnd: int) -> bool:
        """WS_EX_TOOLWINDOW 를 붙인다. 스타일 변경은 숨겼다 다시 보여야 반영된다."""
        if not self._api:
            return False
        _ctypes, user32, _proc = self._api
        try:
            ex = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            new_ex = (ex | _WS_EX_TOOLWINDOW) & ~_WS_EX_APPWINDOW
            user32.ShowWindow(hwnd, _SW_HIDE)
            user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, new_ex)
            # SW_SHOWNA: 다시 보이게 하되 포커스는 뺏지 않는다.
            user32.ShowWindow(hwnd, _SW_SHOWNA)
            return True
        except Exception:
            logger.debug("작업표시줄 숨기기 실패 hwnd=%s", hwnd, exc_info=True)
            return False

    def _sweep(self) -> None:
        for hwnd in self._enum_windows():
            if hwnd in self._known:
                continue  # 사용자가 원래 쓰던 창은 건드리지 않는다
            if hwnd in self._handled and self._is_hidden_from_taskbar(hwnd):
                continue  # 이미 처리했고 스타일도 유지 중
            if self._hide_from_taskbar(hwnd):
                if hwnd not in self._handled:
                    logger.debug("수집용 브라우저 창을 작업표시줄에서 숨김 hwnd=%s", hwnd)
                self._handled.add(hwnd)

    def _run(self) -> None:
        started = time.monotonic()
        while not self._stop.is_set():
            try:
                self._sweep()
            except Exception:
                logger.debug("작업표시줄 감시 중 오류(무시)", exc_info=True)
            fast = (time.monotonic() - started) < _FAST_PHASE_SEC and not self._handled
            self._stop.wait(_POLL_FAST_SEC if fast else _POLL_SLOW_SEC)

    # ── 공개 API ────────────────────────────────────────────────────────
    def start(self) -> None:
        """감시를 시작한다. **브라우저를 띄우기 직전에** 호출할 것."""
        if not self._api:
            return
        if self._thread is not None:
            return
        try:
            # 지금 열려 있는 창들은 사용자 것이므로 기억해 두고 건드리지 않는다.
            self._known = set(self._enum_windows())
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="TaskbarHider", daemon=True
            )
            self._thread.start()
        except Exception:
            logger.debug("작업표시줄 감시 스레드 시작 실패(무시)", exc_info=True)
            self._thread = None

    def stop(self) -> None:
        """감시를 멈춘다. 몇 번을 불러도 안전하다."""
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        self._handled.clear()

    @property
    def hidden_count(self) -> int:
        return len(self._handled)


if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG, format="  %(asctime)s [%(levelname)s] %(message)s")

    print("=== TaskbarHider 자체 테스트 ===")
    h = TaskbarHider()
    print("  Windows API 사용 가능:", h._api is not None)
    h.start()
    print("  감시 시작. 기존 크롬 창", len(h._known), "개는 건드리지 않습니다.")
    time.sleep(2)
    print("  숨긴 창 수:", h.hidden_count, "(새 크롬 창을 안 띄웠으면 0이 정상)")
    h.stop()
    print("  감시 중지 완료")
    print("\nOK: winhide.py self-test passed")
