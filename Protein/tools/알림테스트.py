"""Windows 토스트 알림 진단 도구.

## 왜 필요한가

`core/notifier.py`가 예외 없이 "토스트 알림 표시" 로그를 남기는데도
화면에 아무것도 뜨지 않는 문제가 있었다. 원인은 winotify가 PowerShell
스크립트를 fire-and-forget(`Popen`, stdout/stderr는 DEVNULL)으로 실행하기
때문에 — PowerShell 내부에서 `CreateToastNotifier(app_id)`가 실패해도
(가장 흔한 원인: app_id가 Windows에 AUMID로 등록 안 됨) 그 실패가 Python
쪽에는 절대 전달되지 않는다는 것이다.

`core/notifier.py`는 이제 시작 시 자동으로 app_id를 AUMID로 등록한다.
이 스크립트는 그게 실제로 등록됐는지 확인하고, 그래도 안 뜨는 다른 원인
(집중 지원/방해 금지 모드, 알림 설정 꺼짐 등)까지 점검할 수 있도록
서로 다른 방식으로 토스트 4개를 연달아 띄운다.

## 사용법

    PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools\\알림테스트.py

콘솔에 나오는 안내를 읽고 "1~4번 중 몇 번이 화면에 떴는지" 확인해서
알려주면 원인을 좁힐 수 있다.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 콘솔이 cp949라 한글이 깨질 수 있으므로 utf-8로 강제한다.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from core.notifier import (  # noqa: E402
    DEFAULT_APP_ID,
    POWERSHELL_APP_ID,
    is_aumid_registered,
    register_aumid,
)
from core.paths import ICON_FILE  # noqa: E402


def _line(char: str = "-", n: int = 70) -> None:
    print(char * n)


# ---------------------------------------------------------------------
# 1) AUMID 등록 상태 점검
# ---------------------------------------------------------------------
def check_aumid() -> None:
    _line("=")
    print("[1] AUMID(AppUserModelID) 등록 상태 점검")
    _line("=")

    print(f"  기본 app_id      : {DEFAULT_APP_ID!r}")
    print(f"  아이콘 경로       : {ICON_FILE} (존재: {ICON_FILE.exists()})")

    already = is_aumid_registered(DEFAULT_APP_ID)
    print(f"  등록 상태(등록 전): {'등록됨' if already else '미등록'}")

    ok = register_aumid(DEFAULT_APP_ID, display_name=DEFAULT_APP_ID, icon_path=ICON_FILE)
    print(f"  등록 시도 결과     : {'성공' if ok else '실패'}")

    now = is_aumid_registered(DEFAULT_APP_ID)
    print(f"  등록 상태(등록 후): {'등록됨' if now else '미등록'}")

    print()
    print(f"  참고: 레지스트리 경로")
    print(r"    HKEY_CURRENT_USER\SOFTWARE\Classes\AppUserModelId\{}".format(DEFAULT_APP_ID))
    print("    (여기 DisplayName / IconUri 값이 있으면 등록된 것)")
    print()
    print(f"  폴백용 PowerShell AUMID (이미 Windows에 등록되어 있음): {POWERSHELL_APP_ID}")
    print(f"  이미 등록 상태: {'등록됨' if is_aumid_registered(POWERSHELL_APP_ID) else '미등록(정상 — Windows가 자체 관리)'}")


# ---------------------------------------------------------------------
# 2) 집중 지원 / 방해 금지 모드, 알림 설정 점검 (best-effort)
# ---------------------------------------------------------------------
def check_focus_assist() -> None:
    _line("=")
    print("[2] 집중 지원(Focus Assist) / 알림 설정 점검 (참고용, 확정 불가)")
    _line("=")

    import winreg

    # 2-1) 전역 토스트 스위치: "앱 및 기타 보낸사람의 알림 받기"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
        ) as k:
            try:
                value, _t = winreg.QueryValueEx(k, "NOC_GLOBAL_SETTING_TOASTS_ENABLED")
                state = "켜짐(정상)" if value else "꺼짐 !! 알림이 전역으로 꺼져 있습니다 !!"
                print(f"  전역 알림 스위치(NOC_GLOBAL_SETTING_TOASTS_ENABLED): {value} -> {state}")
            except FileNotFoundError:
                print("  전역 알림 스위치: 레지스트리 값 없음 (한 번도 끈 적 없음 = 기본값 켜짐으로 추정)")
    except Exception as exc:
        print(f"  전역 알림 스위치 확인 실패: {exc}")

    # 2-2) 우리 앱의 AUMID가 Windows 알림 설정에 실제로 등록됐는지
    #      (한 번이라도 토스트가 Show()까지 성공적으로 도달했으면 여기 항목이 생긴다 —
    #       즉 이 항목이 있다는 것 자체가 "OS 파이프라인까지는 도달했다"는 좋은 신호다)
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{}".format(
            DEFAULT_APP_ID
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            info = winreg.QueryInfoKey(k)
            print(f"  '{DEFAULT_APP_ID}' 알림 설정 항목: 존재함 (과거에 최소 1번은 Show()까지 도달했음)")
            for i in range(info[1]):
                name, value, _t = winreg.EnumValue(k, i)
                print(f"    - {name} = {value}")
    except FileNotFoundError:
        print(
            f"  '{DEFAULT_APP_ID}' 알림 설정 항목: 없음 "
            "(아직 한 번도 Show()가 OS까지 도달한 적이 없다는 뜻 — 이 스크립트 실행 후 다시 확인해볼 것)"
        )
    except Exception as exc:
        print(f"  알림 설정 항목 확인 실패: {exc}")

    # 2-3) 집중 지원(방해 금지) 상태 — 정확한 파싱 방법이 공식 문서화되어 있지 않아
    #      바이너리 blob 존재 여부만 알려주고, 최종 판단은 사용자가 눈으로 확인하게 한다.
    try:
        path = (
            r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount"
            r"\Current\default$windows.data.donotdisturb.quiethourssettings"
            r"\windows.data.donotdisturb.quiethourssettings"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            data, _t = winreg.QueryValueEx(k, "Data")
            print(f"  집중 지원 원본 데이터(hex, 참고용): {data.hex()}")
            print(
                "  ※ 이 바이너리 값의 정확한 on/off 인코딩은 공식 문서가 없어 "
                "신뢰할 수 있게 자동 판정할 수 없습니다."
            )
    except Exception:
        print("  집중 지원 상태 레지스트리 값을 읽지 못했습니다 (정상일 수 있음).")

    print()
    print("  *** 가장 확실한 방법: 작업표시줄 시계 근처 '알림' 아이콘을 클릭해서")
    print("      맨 아래 '집중 지원'이 '끄기'로 되어 있는지 눈으로 직접 확인하세요. ***")
    print()
    print("  알림 설정 화면을 바로 열려면 아래 명령을 실행하세요:")
    print("      start ms-settings:notifications")


# ---------------------------------------------------------------------
# 3) 서로 다른 방식으로 토스트 4개 연속 발사
# ---------------------------------------------------------------------
def fire_test_toasts() -> None:
    _line("=")
    print("[3] 서로 다른 방식으로 토스트 4개를 순서대로 띄웁니다")
    _line("=")
    print("  각 토스트 사이에 1.5초씩 간격을 둡니다. 화면 우측 하단(또는 알림 센터)을")
    print("  잘 보고, 어떤 번호가 실제로 '떴는지' 순서대로 기억해 두세요.")
    print()

    from winotify import Notification, audio  # noqa: E402  (지연 import — 진단 목적)

    icon_str = str(ICON_FILE) if ICON_FILE.exists() else ""

    tests = []

    # [1] 등록된 커스텀 AUMID + 아이콘 + 액션 버튼 (core/notifier.py가 평소 쓰는 방식과 동일)
    def t1():
        n = Notification(
            app_id=DEFAULT_APP_ID,
            title="[1/4] 커스텀 AUMID (등록됨)",
            msg="이게 보이면: 커스텀 AUMID 등록이 정상 동작하는 것입니다.",
            icon=icon_str,
        )
        n.add_actions(label="확인", launch="https://example.com")
        n.set_audio(audio.Default, loop=False)
        n.show()

    tests.append(("1", "등록된 커스텀 AUMID(아이콘+액션 버튼 포함)", t1))

    # [2] 이미 Windows에 등록되어 있는 PowerShell AUMID를 빌려 씀
    def t2():
        n = Notification(
            app_id=POWERSHELL_APP_ID,
            title="[2/4] PowerShell AUMID 차용",
            msg="이게 보이면: 커스텀 AUMID 등록엔 문제가 있지만 토스트 자체는 가능합니다.",
            icon=icon_str,
        )
        n.set_audio(audio.Default, loop=False)
        n.show()

    tests.append(("2", "시스템에 이미 등록된 PowerShell AUMID 차용 (\"Windows PowerShell\"로 표시됨)", t2))

    # [3] 커스텀 AUMID, 아이콘 없이
    def t3():
        n = Notification(
            app_id=DEFAULT_APP_ID,
            title="[3/4] 아이콘 없음",
            msg="이게 보이면: 아이콘 경로 문제가 원인이 아니었던 것입니다.",
            icon="",
        )
        n.set_audio(audio.Default, loop=False)
        n.show()

    tests.append(("3", "커스텀 AUMID, 아이콘 없이", t3))

    # [4] 커스텀 AUMID, 액션 버튼/아이콘/사운드 전부 최소화 (가장 단순한 형태)
    def t4():
        n = Notification(
            app_id=DEFAULT_APP_ID,
            title="[4/4] 최소 옵션",
            msg="이게 보이면: 최소 구성에서는 정상 동작합니다.",
            icon="",
        )
        n.show()

    tests.append(("4", "액션 버튼/아이콘/사운드 없이 최소 옵션", t4))

    for label, desc, fn in tests:
        print(f"  -> [{label}] 발사: {desc}")
        try:
            fn()
            print(f"     (예외 없이 PowerShell 호출을 마쳤습니다 — 화면 표시 여부는 별개입니다)")
        except Exception as exc:
            print(f"     !! 예외 발생: {exc!r}")
        time.sleep(1.5)

    print()
    print("  4개 모두 예외 없이 발사되었습니다 (발사 성공 != 화면 표시 성공 — 그게 바로 이 버그입니다).")


def main() -> int:
    print()
    print("#" * 70)
    print("# 프로틴 할인 알림 - Windows 토스트 알림 진단 도구")
    print("#" * 70)
    print()

    check_aumid()
    print()
    check_focus_assist()
    print()
    fire_test_toasts()

    print()
    _line("=")
    print("[4] 결과 확인 부탁드립니다")
    _line("=")
    print("  방금 뜬 토스트 중 실제로 화면에 '보인' 번호가 몇 번인가요? (예: '1, 2번 보임' / '하나도 안 보임')")
    print("  - 1번만 안 보이고 2번은 보였다면        -> 커스텀 AUMID 등록에 여전히 문제가 있습니다.")
    print("  - 1, 2번 모두 안 보이고 3, 4번은 보였다면 -> 아이콘/액션 버튼이 원인입니다.")
    print("  - 4개 다 안 보였다면                     -> 집중 지원/방해 금지 모드나 알림 설정 자체가 꺼져")
    print("                                              있을 가능성이 높습니다. 위 [2] 안내를 참고해")
    print("                                              'start ms-settings:notifications' 로 직접 확인하세요.")
    print("  - 4개 다 보였다면                        -> core/notifier.py의 AUMID 자동 등록으로 문제가")
    print("                                              해결된 것입니다.")
    print()
    print("  알림 센터(작업표시줄 시계 클릭)에는 화면에 안 뜬 토스트도 '누적'되어 남아있을 수 있으니")
    print("  그것도 함께 확인해 주세요.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
