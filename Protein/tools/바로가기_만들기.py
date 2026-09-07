"""바로가기(.lnk) 만들기 — 바탕화면 / 시작 메뉴 / 자동 시작.

## 이게 왜 필요한가

윈도우 **시작 메뉴 검색은 설치 여부와 무관하게 시작 메뉴 폴더 안의 ``.lnk`` 파일을
색인**한다. 즉 프로그램을 "설치"하지 않아도 바로가기 하나만 올려두면
시작 버튼 → "프로틴" 입력으로 찾을 수 있다. 바탕화면도 마찬가지다.

## 사용법

    python tools\\바로가기_만들기.py                  # 바탕화면 + 시작 메뉴에 만들기
    python tools\\바로가기_만들기.py --자동시작        # 위 + 윈도우 켤 때 자동 실행까지
    python tools\\바로가기_만들기.py --상태            # 지금 어디에 등록돼 있는지 확인
    python tools\\바로가기_만들기.py --제거            # 세 곳 모두에서 제거

개별 지정도 된다: ``--바탕화면`` ``--시작메뉴`` ``--자동시작``
(``--desktop`` ``--startmenu`` ``--startup`` ``--status`` ``--remove`` 도 동일하게 동작)

## 구현 메모

* ``.lnk`` 생성은 파워셸의 ``WScript.Shell`` COM으로 한다. 외부 패키지(pywin32 등)가
  필요 없다.
* 폴더 위치는 **반드시 윈도우 특수 폴더 API로 얻는다.** 이 PC만 해도 바탕화면이
  ``D:\\Users\\Y16083\\Desktop`` 으로 옮겨져 있어서, ``%USERPROFILE%\\Desktop`` 로
  가정하면 엉뚱한 곳에 만들어진다.
* 아이콘은 ``ui/icon.ico`` 를 지정한다. 지정하지 않으면 대상이 ``pythonw.exe`` 라서
  **파이썬 아이콘**으로 보인다.
* 경로에 한글·공백이 있으므로 파워셸에 넘길 때 따옴표 처리를 꼼꼼히 한다.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

SHORTCUT_NAME = "프로틴 할인 알림.lnk"
MAIN_PY = ROOT_DIR / "main.py"
ICON = ROOT_DIR / "ui" / "icon.ico"
DESCRIPTION = "쿠팡 프로틴 가격을 감시하고 할인 시 알림"

# (키, 표시 이름, 특수 폴더 이름 또는 특수 처리)
TARGETS = {
    "desktop": ("바탕화면", "Desktop"),
    "startmenu": ("시작 메뉴", "Programs"),
    "startup": ("자동 시작", "Startup"),
}


def _ps(script: str, timeout: int = 30) -> tuple[int, str, str]:
    """파워셸을 실행하고 (반환코드, 표준출력, 표준오류)를 돌려준다."""
    # 파워셸은 기본적으로 콘솔 코드페이지(cp949)로 출력해서 한글 경로가 깨진다.
    # 스크립트 앞에 출력 인코딩을 UTF-8로 바꾸는 구문을 붙여 준다.
    script = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + script
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _q(value: str) -> str:
    """파워셸 작은따옴표 문자열용 이스케이프."""
    return str(value).replace("'", "''")


def special_folder(name: str) -> Path | None:
    """윈도우 특수 폴더의 실제 경로를 얻는다.

    바탕화면이 다른 드라이브로 옮겨져 있는 경우가 흔해서 환경변수로 조합하면 안 된다.
    """
    rc, out, _ = _ps(f"[Environment]::GetFolderPath('{_q(name)}')")
    if rc == 0 and out:
        return Path(out)
    return None


def shortcut_path(key: str) -> Path | None:
    label, folder_name = TARGETS[key]
    folder = special_folder(folder_name)
    if folder is None:
        return None
    return folder / SHORTCUT_NAME


def pythonw_exe() -> Path:
    """콘솔 창 없이 띄우는 pythonw.exe 경로. 없으면 현재 인터프리터."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def create(key: str) -> bool:
    path = shortcut_path(key)
    label = TARGETS[key][0]
    if path is None:
        print(f"  [{label}] ❌ 폴더 위치를 찾지 못했습니다.")
        return False

    target = pythonw_exe()
    icon_part = ""
    if ICON.exists():
        # ',0' = 파일의 첫 번째 아이콘
        icon_part = f"$s.IconLocation = '{_q(str(ICON))},0'; "
    else:
        print(f"  [{label}] ⚠ 아이콘 파일이 없어 기본(파이썬) 아이콘으로 만들어집니다: {ICON}")

    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{_q(str(path))}'); "
        f"$s.TargetPath = '{_q(str(target))}'; "
        f"$s.Arguments = '\"{_q(str(MAIN_PY))}\"'; "
        f"$s.WorkingDirectory = '{_q(str(ROOT_DIR))}'; "
        f"$s.Description = '{_q(DESCRIPTION)}'; "
        f"{icon_part}"
        "$s.WindowStyle = 7; "
        "$s.Save(); "
        "Write-Output 'OK'"
    )
    rc, out, err = _ps(script)
    if rc == 0 and "OK" in out and path.exists():
        print(f"  [{label}] ✅ 생성: {path}")
        return True
    print(f"  [{label}] ❌ 실패 (rc={rc}) {err[:200]}")
    return False


def remove(key: str) -> bool:
    path = shortcut_path(key)
    label = TARGETS[key][0]
    if path is None:
        print(f"  [{label}] ❌ 폴더 위치를 찾지 못했습니다.")
        return False
    if not path.exists():
        print(f"  [{label}] · 이미 없음")
        return True
    try:
        path.unlink()
        print(f"  [{label}] ✅ 제거: {path}")
        return True
    except Exception as e:
        print(f"  [{label}] ❌ 제거 실패: {type(e).__name__}: {e}")
        return False


def status() -> None:
    print("현재 등록 상태:")
    for key in TARGETS:
        label = TARGETS[key][0]
        path = shortcut_path(key)
        if path is None:
            print(f"  [{label}] ? 폴더를 찾지 못함")
            continue
        mark = "✅ 등록됨" if path.exists() else "· 없음"
        print(f"  [{label}] {mark}")
        print(f"        {path}")


def verify(key: str) -> None:
    """만들어진 바로가기의 속성을 실제로 읽어 확인한다."""
    path = shortcut_path(key)
    if path is None or not path.exists():
        return
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{_q(str(path))}'); "
        "Write-Output ('TARGET=' + $s.TargetPath); "
        "Write-Output ('ARGS=' + $s.Arguments); "
        "Write-Output ('WORKDIR=' + $s.WorkingDirectory); "
        "Write-Output ('ICON=' + $s.IconLocation)"
    )
    rc, out, _ = _ps(script)
    if rc == 0:
        for line in out.splitlines():
            print(f"        {line}")


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]

    print("=" * 66)
    print("  프로틴 할인 알림 — 바로가기 만들기")
    print("=" * 66)
    print(f"  실행 대상 : {pythonw_exe()}")
    print(f"  스크립트  : {MAIN_PY}")
    print(f"  아이콘    : {ICON}  (존재: {ICON.exists()})")
    print()

    if not MAIN_PY.exists():
        print(f"❌ main.py 를 찾을 수 없습니다: {MAIN_PY}")
        return 1

    if "--상태" in args or "--status" in args:
        status()
        return 0

    picked = []
    if "--바탕화면" in args or "--desktop" in args:
        picked.append("desktop")
    if "--시작메뉴" in args or "--startmenu" in args:
        picked.append("startmenu")
    if "--자동시작" in args or "--startup" in args:
        picked.append("startup")

    removing = "--제거" in args or "--remove" in args

    if removing:
        keys = picked or list(TARGETS)
        print("바로가기 제거:")
        ok = all(remove(k) for k in keys)
        print()
        status()
        return 0 if ok else 1

    # 기본값: 바탕화면 + 시작 메뉴 (자동 시작은 명시해야 등록)
    keys = picked or ["desktop", "startmenu"]
    print("바로가기 생성:")
    results = []
    for k in keys:
        created = create(k)
        results.append(created)
        if created:
            verify(k)
    print()
    if all(results):
        print("완료했습니다.")
        print("  · 시작 버튼을 누르고 '프로틴' 을 입력하면 검색됩니다.")
        print("    (검색 색인에 반영되기까지 잠깐 걸릴 수 있습니다)")
        if "startup" not in keys:
            print("  · 윈도우 켤 때 자동 실행까지 원하면: --자동시작 옵션을 붙여 다시 실행하세요.")
        return 0
    print("일부 항목을 만들지 못했습니다. 위 메시지를 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
