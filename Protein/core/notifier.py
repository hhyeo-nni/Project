"""Windows 토스트 알림 — SPEC.md 5절.

winotify를 쓰지 않고 PowerShell(Windows Toast API)을 직접 호출해 토스트를
띄운다. 실패해도 앱이 죽으면 안 되므로 모든 notify 계열 메서드는 예외를
절대 밖으로 던지지 않고 로깅만 한다.

토스트 XML은 ``ToastGeneric`` 템플릿으로 직접 만들고(``_build_toast_xml``),
PowerShell 스크립트를 **UTF-8 BOM 파일(.ps1)로 저장해 ``-File`` 로 실행**한다
(``_show_toast``). 그 외 지켜야 할 것들:
- 아이콘 경로는 반드시 절대경로여야 한다(상대경로는 표시가 안 됨).
- 연속으로 여러 토스트를 너무 빠르게 띄우면 Windows 알림 센터가 일부를
  누락시키는 경우가 있어, 호출 간 최소 0.5초 간격을 내부적으로 보장한다.

과거 이력 — winotify를 버린 이유 (2026-09-03/04 실측)
------------------------------------------------------
원래는 ``winotify`` 패키지를 썼는데, ``toast.show()`` 가 예외 없이 "성공"해도
화면에 토스트가 전혀 뜨지 않거나 내용 없는 "새 알림"만 뜨는 현상이 있었다.
원인은 winotify 내부 구현의 구조적 결함 두 가지였다:

1. ``winotify/__init__.py`` 의 ``show()`` 는 토스트 스크립트를
   ``powershell -Command <스크립트 전체>`` 형태로 **명령줄 인자**로 넘긴다.
   그런데 그 스크립트 안에는 PowerShell **here-string(``@"`` … ``"@``)** 이
   들어 있고, 이 문법은 줄바꿈 위치가 정확해야 한다. 명령줄로 넘기면 깨져서
   ``$Template`` 이 망가지고 → XML이 비어서 → Windows가 내용 없는
   **"새 알림"** 으로 표시한다.
2. ``_run_ps()`` 가 ``subprocess.Popen(..., stdout=DEVNULL, stderr=DEVNULL)`` 로
   **fire-and-forget** 실행한다. 그래서 PowerShell이 내부적으로
   ``[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(app_id)``
   호출에서 예외를 던지더라도(가장 흔한 원인: ``app_id`` 가 Windows에
   AUMID(AppUserModelID)로 등록돼 있지 않음) 그 실패는 파이썬에 전혀 전달되지
   않고 조용히 사라진다 — Python 쪽에서는 항상 "성공"으로 보인다.

그래서 이 모듈은 winotify를 더 이상 쓰지 않는다(아래 ``_show_toast`` 참고).
AUMID 자동 등록(``register_aumid()``, 관리자 권한 불필요, 멱등)은 새 구현에도
그대로 유지된다: 앱 시작 시 ``app_id`` 를
``HKCU\\SOFTWARE\\Classes\\AppUserModelId\\<app_id>`` 레지스트리에
``DisplayName`` / ``IconUri`` 값과 함께 등록한다. 그래도 안 뜨는 경우를 위해
``config.json`` 의 ``notify_app_id`` 값으로 이미 시스템에 등록되어 있는 다른
AUMID(예: PowerShell 자체의 AUMID, ``POWERSHELL_APP_ID``)를 빌려 쓸 수 있게
폴백 경로를 열어 두었다. 자세한 진단 절차는 ``tools/알림테스트.py`` 참고.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

from core.models import AlertEvent
from core.paths import ICON_FILE, ICON_ICO

logger = logging.getLogger(__name__)

_TITLE_MAX_LEN = 40
_MIN_NOTIFY_INTERVAL_SECONDS = 0.5


def _x(value: str) -> str:
    """XML 속성/텍스트에 안전하게 넣을 수 있게 이스케이프한다."""
    return _xml_escape(str(value or ""), {'"': "&quot;", "'": "&apos;"})


# 토스트를 띄우는 PowerShell 스크립트. **파일로 저장해서 -File 로 실행**한다.
# (winotify처럼 -Command 로 통째로 넘기면 here-string 문법이 깨져 빈 토스트가 뜬다)
_PS_TEMPLATE = """$ErrorActionPreference = "Stop"
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

$xml = @'
@@XML@@
'@

$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("@@APPID@@")
$notifier.Show($toast)
Write-Output "TOAST_OK"
"""


# 기본 앱 표시 이름 / AUMID. Notifier(app_id=...)로 명시적으로 넘기지 않으면
# 이 값을 쓰되, config.json의 notify_app_id가 설정돼 있으면 그것을 우선한다.
DEFAULT_APP_ID = "프로틴 할인 알림"

# 이미 Windows에 기본 등록되어 있는 PowerShell(v1.0)의 AUMID.
# 커스텀 AUMID 등록이 이런저런 이유로 안 먹힐 때 config.json의 notify_app_id에
# 이 값을 넣으면 알림에 "Windows PowerShell"이라는 이름으로 뜨지만 확실히 표시된다.
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def register_aumid(app_id: str, display_name: str, icon_path: Optional[Path] = None) -> bool:
    """``app_id`` 를 AUMID로 HKCU 레지스트리에 등록한다.

    ``HKEY_CURRENT_USER\\SOFTWARE\\Classes\\AppUserModelId\\<app_id>`` 아래에
    ``DisplayName`` / ``IconUri`` 값을 만든다. HKCU라 관리자 권한이 필요 없고,
    이미 등록되어 있으면 값을 덮어쓰기만 하므로 멱등하다.

    실패해도(비-Windows 환경, 권한 문제 등) 예외를 밖으로 던지지 않고
    False를 반환한다.
    """
    try:
        import winreg
    except ImportError:
        logger.debug("winreg를 사용할 수 없는 환경입니다 (Windows가 아님) — AUMID 등록 건너뜀")
        return False

    try:
        key_path = f"SOFTWARE\\Classes\\AppUserModelId\\{app_id}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
            if icon_path is not None:
                icon_str = str(icon_path)
                try:
                    if Path(icon_str).exists():
                        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon_str)
                except Exception:
                    pass
        logger.info("AUMID 등록/갱신 완료: %s (DisplayName=%s)", app_id, display_name)
        return True
    except Exception:
        logger.exception("AUMID 레지스트리 등록 실패 (알림 표시가 안 될 수 있음): %s", app_id)
        return False


def set_process_app_id(app_id: str = DEFAULT_APP_ID) -> bool:
    """이 프로세스의 AppUserModelID를 지정한다.

    **작업표시줄 아이콘 문제의 해결책이다.** 윈도우는 작업표시줄 버튼을
    "창"이 아니라 "프로세스의 AUMID" 기준으로 묶고 이름·아이콘을 정한다.
    AUMID를 지정하지 않으면 실행 파일(``pythonw.exe``)의 것을 그대로 쓰기 때문에,
    창에 아이콘을 아무리 잘 걸어도 작업표시줄에는 **파이썬 아이콘**이 뜬다.

    반드시 **창을 하나라도 만들기 전에**(프로그램 시작 직후) 불러야 한다.
    이미 창이 만들어진 뒤에 부르면 그 창에는 반영되지 않는다.

    실패해도 예외를 던지지 않는다(아이콘이 예쁘지 않은 것보다 앱이 죽는 게 나쁘다).
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        logger.info("프로세스 AppUserModelID 설정: %s", app_id)
        return True
    except Exception:
        logger.debug("프로세스 AppUserModelID 설정 실패(무시)", exc_info=True)
        return False


def get_process_app_id() -> Optional[str]:
    """현재 프로세스에 설정된 AppUserModelID를 돌려준다(검증용). 없으면 None."""
    try:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(buf))
        if hr == 0 and buf.value:
            value = buf.value
            ctypes.windll.ole32.CoTaskMemFree(buf)
            return value
    except Exception:
        logger.debug("AppUserModelID 조회 실패", exc_info=True)
    return None


def is_aumid_registered(app_id: str) -> bool:
    """``app_id`` 가 이미 AUMID로 등록되어 있는지(DisplayName 값 존재 여부) 확인한다."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        key_path = f"SOFTWARE\\Classes\\AppUserModelId\\{app_id}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "DisplayName")
            return bool(value)
    except Exception:
        return False


def _notify_app_id_from_config() -> Optional[str]:
    """config.json의 notify_app_id를 읽어온다. 실패하면 None(=기본값 사용)."""
    try:
        from core.config import load_config

        cfg = load_config()
        value = getattr(cfg, "notify_app_id", None)
        if isinstance(value, str) and value.strip():
            return value
    except Exception:
        logger.debug("config.json에서 notify_app_id 조회 실패 — 기본값 사용", exc_info=True)
    return None


class Notifier:
    """PowerShell(Windows Toast API) 직접 호출 기반 Windows 토스트 알림 래퍼.

    winotify는 쓰지 않는다 — 모듈 docstring의 "과거 이력" 참고.
    """

    def __init__(
        self,
        app_id: Optional[str] = None,
        icon_path: Optional[Path] = None,
        sound: bool = False,
    ) -> None:
        """
        Args:
            app_id: 토스트의 AUMID/표시 이름. 명시적으로 넘기지 않으면(None, 기존
                기본 동작과 호환) config.json의 ``notify_app_id`` 값을 먼저 확인하고,
                없으면 ``DEFAULT_APP_ID``("프로틴 할인 알림")를 쓴다. 이미 시스템에
                등록된 다른 AUMID를 빌려 쓰려면 ``POWERSHELL_APP_ID`` 를 명시적으로
                넘기거나 config.json에 ``notify_app_id`` 를 설정하면 된다.
            icon_path: 알림 아이콘 절대경로. 생략 시 ``ui/icon.png``.
            sound: 알림 소리 재생 여부.
        """
        if app_id is None:
            app_id = _notify_app_id_from_config() or DEFAULT_APP_ID
        self.app_id = app_id
        # 아이콘은 절대경로가 아니면 토스트에 표시가 안 되므로 항상 절대경로로 보정한다.
        resolved_icon = icon_path if icon_path is not None else ICON_FILE
        try:
            resolved_icon = Path(resolved_icon).resolve()
        except Exception:
            logger.exception("아이콘 경로 처리 실패: %s", icon_path)
        self.icon_path = resolved_icon
        self.sound = sound

        # 연속 호출 간 최소 간격을 보장하기 위한 락과 마지막 호출 시각.
        self._lock = threading.Lock()
        self._last_notify_ts = 0.0

        if not self.icon_path.exists():
            logger.warning("알림 아이콘 파일을 찾을 수 없습니다: %s", self.icon_path)

        # 토스트가 실제로 화면에 뜨려면 app_id가 Windows에 AUMID로 등록되어 있어야
        # 한다(등록 안 돼 있으면 CreateToastNotifier() 호출이 PowerShell 쪽에서
        # 예외로 실패한다 — _show_toast()가 stderr를 확인해 로그로 남긴다).
        # 이미 등록된 AUMID(예: config.json으로 빌려 쓴 POWERSHELL_APP_ID처럼
        # Windows/다른 프로그램이 이미 등록해 둔 것)는 건드리지 않는다 — 우리
        # 것이 아닌 항목의 DisplayName/IconUri를 덮어쓰지 않기 위해서다.
        # 실패해도 앱 실행에는 영향이 없어야 하므로 예외를 삼킨다.
        try:
            # 우리 소유의 AUMID면 매번 갱신한다. 아이콘 파일을 바꿔도 반영되어야
            # 하기 때문이다(등록 여부만 보면 예전 아이콘 경로가 계속 남는다).
            # 반대로 config.json으로 빌려 쓴 남의 AUMID(예: PowerShell)는
            # DisplayName/IconUri를 덮어쓰면 안 되므로 건드리지 않는다.
            if self.app_id == DEFAULT_APP_ID or not is_aumid_registered(self.app_id):
                # IconUri 는 작업표시줄/알림 양쪽에서 쓰이므로, 멀티 해상도인
                # .ico 가 있으면 그쪽을 우선한다(작은 크기에서 더 또렷하다).
                reg_icon = ICON_ICO if ICON_ICO.exists() else self.icon_path
                register_aumid(
                    self.app_id,
                    display_name=DEFAULT_APP_ID,
                    icon_path=reg_icon if reg_icon.exists() else None,
                )
        except Exception:
            logger.exception("Notifier 초기화 중 AUMID 등록 단계에서 예외 발생 (무시)")

    # ------------------------------------------------------------------
    # 내부 유틸
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        """직전 알림 이후 최소 간격이 지날 때까지 대기한다(알림 유실 방지)."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_notify_ts
            wait = _MIN_NOTIFY_INTERVAL_SECONDS - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_notify_ts = time.monotonic()

    @staticmethod
    def _truncate_title(title: str, max_len: int = _TITLE_MAX_LEN) -> str:
        if len(title) <= max_len:
            return title
        return title[: max_len - 1] + "…"

    def _icon_str(self) -> str:
        # 아이콘 파일이 없으면 빈 문자열을 반환한다 — _build_toast_xml()이
        # 이 경우 appLogoOverride <image> 태그 자체를 생략한다.
        return str(self.icon_path) if self.icon_path.exists() else ""

    def _build_toast_xml(
        self,
        title: str,
        message: str,
        detail: str = "",
        launch_url: str = "",
        action_label: Optional[str] = None,
        action_url: Optional[str] = None,
    ) -> str:
        """토스트 XML을 만든다. ToastGeneric은 본문을 3줄까지 보여줄 수 있다."""
        icon = self._icon_str()
        img = (
            f'<image placement="appLogoOverride" hint-crop="circle" src="{_x(icon)}"/>'
            if icon else ""
        )
        lines = f"<text>{_x(title)}</text><text>{_x(message)}</text>"
        if detail:
            lines += f"<text>{_x(detail)}</text>"

        launch_attr = (
            f' activationType="protocol" launch="{_x(launch_url)}"' if launch_url else ""
        )
        actions = ""
        if action_label and action_url:
            actions = (
                f'<actions><action content="{_x(action_label)}" '
                f'activationType="protocol" arguments="{_x(action_url)}"/></actions>'
            )
        audio_tag = "" if self.sound else '<audio silent="true"/>'

        return (
            f'<toast{launch_attr} duration="long">'
            f'<visual><binding template="ToastGeneric">{img}{lines}</binding></visual>'
            f"{actions}{audio_tag}"
            f"</toast>"
        )

    def _show_toast(
        self,
        title: str,
        message: str,
        detail: str = "",
        launch_url: str = "",
        action_label: Optional[str] = None,
        action_url: Optional[str] = None,
    ) -> None:
        """PowerShell 스크립트 **파일**로 토스트를 띄운다.

        winotify를 쓰지 않는 이유(2026-09-04 실측):
        winotify는 스크립트를 ``powershell -Command <스크립트 전체>`` 로 넘기는데,
        그 스크립트에는 PowerShell here-string(``@"`` … ``"@``)이 들어 있다.
        here-string은 줄바꿈 위치가 정확해야 하는 문법이라 명령줄로 넘기면 깨지고,
        그 결과 XML이 비어서 **내용 없는 "새 알림"** 만 뜬다. 게다가 winotify는
        stdout/stderr를 버리고 종료도 기다리지 않아서 이 실패가 전혀 드러나지 않는다.

        그래서 여기서는:
          * 스크립트를 **UTF-8 BOM 파일**로 쓰고 ``-File`` 로 실행한다
            (BOM이 없으면 PowerShell이 한글을 ANSI로 오해한다)
          * ``capture_output`` 으로 **stderr를 반드시 확인**해 실패를 로그에 남긴다
        """
        self._throttle()
        script_path = None
        try:
            xml = self._build_toast_xml(
                title, message, detail, launch_url, action_label, action_url
            )
            script = _PS_TEMPLATE.replace("@@XML@@", xml).replace("@@APPID@@", self.app_id)

            fd, script_path = tempfile.mkstemp(suffix=".ps1", prefix="protein_toast_")
            with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                f.write(script)

            proc = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", script_path,
                ],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and "TOAST_OK" in (proc.stdout or ""):
                logger.info("토스트 알림 표시: %s / %s", title, message)
            else:
                err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:400]
                logger.error("토스트 알림 실패(rc=%s): %s | %s / %s",
                             proc.returncode, err, title, message)
                print(f"[알림 실패] {title}: {message}")
        except Exception:
            # 알림 실패로 앱이 죽으면 안 된다.
            logger.exception("토스트 알림 표시 중 예외: %s / %s", title, message)
            print(f"[알림 실패 폴백] {title}: {message}")
        finally:
            if script_path:
                try:
                    os.remove(script_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # 공개 API (SPEC.md 5절)
    # ------------------------------------------------------------------
    def notify(self, ev: AlertEvent) -> None:
        """AlertEvent를 토스트로 표시한다. 클릭/버튼 클릭 시 ev.url 을 연다."""
        try:
            title = self._truncate_title(ev.product_name)
            self._show_toast(
                title=title,
                message=ev.message,
                detail=getattr(ev, "detail", "") or "",
                launch_url=ev.url,
                action_label="상품 열기",
                action_url=ev.url,
            )
        except Exception:
            logger.exception("notify() 처리 중 예외 발생 (무시하고 계속 진행): %s", ev)

    def notify_error(self, title: str, message: str) -> None:
        try:
            self._show_toast(title=self._truncate_title(title), message=message)
        except Exception:
            logger.exception("notify_error() 처리 중 예외 발생 (무시하고 계속 진행)")

    def notify_info(self, title: str, message: str) -> None:
        try:
            self._show_toast(title=self._truncate_title(title), message=message)
        except Exception:
            logger.exception("notify_info() 처리 중 예외 발생 (무시하고 계속 진행)")


if __name__ == "__main__":
    from datetime import datetime

    from core.logging_setup import setup_logging
    from core.models import RuleKind

    setup_logging()

    notifier = Notifier(sound=False)

    print(f"app_id: {notifier.app_id}")
    print(f"AUMID 등록 상태: {'등록됨' if is_aumid_registered(notifier.app_id) else '미등록'}")

    sample_events = [
        AlertEvent(
            product_id="1001",
            product_name="마이프로틴 임팩트 웨이 1kg 초콜릿",
            kind=RuleKind.TARGET_PRICE,
            fired_at=datetime.now(),
            price=41900,
            prev_price=52900,
            message="52,900원 → 41,900원 (▼21%) · 목표가 45,000원 도달",
            url="https://www.coupang.com/vp/products/1234567890?itemId=1&vendorItemId=2",
        ),
        AlertEvent(
            product_id="1002",
            product_name="옵티멈뉴트리션 골드스탠다드 100% 웨이 2lbs 더블리치초콜릿",
            kind=RuleKind.LOWEST_EVER,
            fired_at=datetime.now(),
            price=39900,
            prev_price=42000,
            message="39,900원 · 역대 최저가 갱신! (이전 최저 42,000원)",
            url="https://www.coupang.com/vp/products/2345678901?itemId=3&vendorItemId=4",
        ),
        AlertEvent(
            product_id="1003",
            product_name="바디빌더스가든 웨이 프로틴 아이솔레이트 2kg",
            kind=RuleKind.RESTOCK,
            fired_at=datetime.now(),
            price=68000,
            prev_price=None,
            message="품절 → 재입고! 현재가 68,000원",
            url="https://www.coupang.com/vp/products/3456789012?itemId=5&vendorItemId=6",
        ),
    ]

    print(f"아이콘 경로: {notifier.icon_path} (존재: {notifier.icon_path.exists()})")

    for i, ev in enumerate(sample_events, start=1):
        print(f"[{i}/{len(sample_events)}] 토스트 발송 시도: {ev.product_name} / {ev.message}")
        notifier.notify(ev)

    notifier.notify_info("프로틴 할인 알림", "알림 시스템 자체 테스트가 완료되었습니다.")

    print("OK: notifier.py self-test 완료 (실제 예외가 발생하지 않았다면 성공)")
