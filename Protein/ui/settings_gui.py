"""설정 GUI 창 (SPEC.md 참고, tkinter).

트레이 아이콘(pystray) 스레드 등 임의의 스레드에서 open_settings()를 호출해도
안전하도록, 실제 Tk() 생성과 mainloop 실행은 이 모듈이 새로 만드는 전용 스레드
안에서만 수행한다. 이미 창이 열려 있으면 새로 만들지 않고 기존 창을 앞으로
가져온다(모듈 전역 싱글턴 + Lock).
"""

from __future__ import annotations

import logging
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from core.config import AppConfig, load_config, product_of, save_config
from core.models import RuleKind
from core.paths import CONFIG_FILE, ICON_FILE, ICON_ICO
from core.store import Store

try:
    import sv_ttk
except ImportError:  # pragma: no cover - 방어적: 설치 안 된 환경에서도 창은 뜨게 한다.
    sv_ttk = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# 설정 창에서 고를 수 있는 글꼴/크기 목록 (이 PC에 실제로 설치돼 있는 것만).
# 글꼴 후보는 **한글 글리프가 있는 것만** 넣는다.
# "Segoe UI" / "Segoe UI Variable" 계열은 한글이 없어서, 고르면 한글만 굴림으로
# 대체되어 화면이 뒤죽박죽이 된다(실제로 그 증상이 보고됐다). 절대 넣지 말 것.
AVAILABLE_FONT_FAMILIES: list[str] = [
    "Noto Sans KR",      # 가장 현대적. Windows 11 / Office 설치 시 함께 깔리는 경우가 많다
    "맑은 고딕",           # 윈도우 기본 한글 UI 글꼴 (항상 존재)
    "맑은 고딕 Semilight",
    "함초롬돋움",
    "돋움",
]

# 설치되어 있지 않은 글꼴을 고르면 엉뚱하게 대체되므로, 실제 설치된 것만 남긴다.
FALLBACK_FONT_FAMILY = "맑은 고딕"


def installed_font_families(root: Optional[tk.Misc] = None) -> list[str]:
    """AVAILABLE_FONT_FAMILIES 중 이 PC에 실제로 설치된 것만 돌려준다."""
    try:
        installed = set(tkfont.families(root))
    except Exception:
        return [FALLBACK_FONT_FAMILY]
    found = [f for f in AVAILABLE_FONT_FAMILIES if f in installed]
    return found or [FALLBACK_FONT_FAMILY]


# 설치는 돼 있지만 **한글 글리프가 없는** 글꼴들. 이걸 고르면 한글만 굴림으로
# 대체되어 화면이 뒤죽박죽이 된다. 예전 설정에 남아 있더라도 쓰지 않는다.
LATIN_ONLY_FONT_PREFIXES: tuple[str, ...] = (
    "Segoe UI", "Arial", "Tahoma", "Verdana", "Calibri", "Cambria",
    "Times New Roman", "Courier New", "Consolas", "Helvetica",
)


def resolve_font_family(family: str, root: Optional[tk.Misc] = None) -> str:
    """설정된 글꼴이 못 쓸 것이면(미설치이거나 한글이 없으면) 대체한다."""
    fallback = installed_font_families(root)[0]
    if not family:
        return fallback
    if any(family.startswith(p) for p in LATIN_ONLY_FONT_PREFIXES):
        logger.info("한글이 없는 글꼴이라 '%s' 로 대체합니다: %s", fallback, family)
        return fallback
    try:
        if family in set(tkfont.families(root)):
            return family
    except Exception:
        pass
    return fallback


AVAILABLE_FONT_SIZES: list[int] = [9, 10, 11, 12, 13]

# sv-ttk는 자체 명명 글꼴(SunValley*)을 만들어 위젯 스타일에 직접 박아 넣는다.
# 그 기본값이 "Segoe UI Variable ..." 인데 **이 글꼴에는 한글 글리프가 없어서**
# 한글이 굴림으로 대체돼 버린다. 그래서 테마를 입힌 뒤 이 글꼴들을 우리 글꼴로
# 다시 설정해 줘야 표·버튼·체크박스까지 전부 같은 글꼴로 나온다.
# (이름, 기준 크기에 더할 값, 굵게 여부)
SV_TTK_FONTS: list[tuple[str, int, bool]] = [
    ("SunValleyCaptionFont", -1, False),
    ("SunValleyBodyFont", 0, False),
    ("SunValleyBodyStrongFont", 0, True),
    ("SunValleyBodyLargeFont", 3, False),
    ("SunValleySubtitleFont", 5, True),
    ("SunValleyTitleFont", 9, True),
    ("SunValleyTitleLargeFont", 16, True),
    ("SunValleyDisplayFont", 28, True),
]

# 글꼴을 직접 지정해 줘야 하는 ttk 스타일들(테마가 자체 글꼴을 박아두는 곳).
TTK_FONT_STYLES: tuple[str, ...] = (
    ".", "TLabel", "TButton", "Accent.TButton", "TCheckbutton", "TRadiobutton",
    "TEntry", "TCombobox", "TSpinbox", "Treeview", "TNotebook.Tab", "Toggle.TButton",
    "Switch.TCheckbutton", "TLabelframe",
)


# ---------------------------------------------------------------------------
# 쿠팡 URL 파싱
# ---------------------------------------------------------------------------

_PRODUCT_ID_RE = re.compile(r"/vp/products/(\d+)")
_ITEM_ID_RE = re.compile(r"[?&]itemId=(\d+)")
_VENDOR_ITEM_ID_RE = re.compile(r"[?&]vendorItemId=(\d+)")


def parse_coupang_url(url: str) -> Optional[dict[str, Optional[str]]]:
    """쿠팡 상품 URL에서 product_id/item_id/vendor_item_id를 추출한다.

    /vp/products/{id} 패턴이 없으면 None을 반환한다(쿠팡 상품 URL이 아님).
    모바일(m.coupang.com), 추적 파라미터가 붙은 광고 URL도 이 패턴만 있으면
    정상 동작한다. 단축 링크(link.coupang.com)처럼 /vp/products/ 경로 자체가
    없는 URL은 파싱할 수 없다(리다이렉트 해석은 이 함수의 책임 밖).
    """
    if not url:
        return None
    m = _PRODUCT_ID_RE.search(url)
    if not m:
        return None
    item_m = _ITEM_ID_RE.search(url)
    vendor_m = _VENDOR_ITEM_ID_RE.search(url)
    return {
        "product_id": m.group(1),
        "item_id": item_m.group(1) if item_m else None,
        "vendor_item_id": vendor_m.group(1) if vendor_m else None,
    }


def build_canonical_url(
    product_id: str,
    item_id: Optional[str] = None,
    vendor_item_id: Optional[str] = None,
) -> str:
    """product_id/item_id/vendor_item_id로 정규화된 쿠팡 상품 URL을 만든다."""
    url = f"https://www.coupang.com/vp/products/{product_id}"
    params = []
    if item_id:
        params.append(f"itemId={item_id}")
    if vendor_item_id:
        params.append(f"vendorItemId={vendor_item_id}")
    if params:
        url += "?" + "&".join(params)
    return url


# ---------------------------------------------------------------------------
# 규칙 라벨
# ---------------------------------------------------------------------------

RULE_KINDS_ORDER: list[RuleKind] = [
    RuleKind.TARGET_PRICE,
    RuleKind.DISCOUNT_RATE,
    RuleKind.DISCOUNT_SPIKE,
    RuleKind.LOWEST_EVER,
    RuleKind.RESTOCK,
    RuleKind.UNIT_PRICE,
]

RULE_KIND_LABELS: dict[RuleKind, str] = {
    RuleKind.TARGET_PRICE: "목표가 이하",
    RuleKind.DISCOUNT_RATE: "할인율 N% 이상",
    RuleKind.DISCOUNT_SPIKE: "할인율 급등 +N%p",
    RuleKind.LOWEST_EVER: "역대 최저가 갱신",
    RuleKind.RESTOCK: "재입고",
    RuleKind.UNIT_PRICE: "단백질 1g당 단가 이하",
}

RULE_KIND_UNITS: dict[RuleKind, str] = {
    RuleKind.TARGET_PRICE: "원",
    RuleKind.DISCOUNT_RATE: "%",
    RuleKind.DISCOUNT_SPIKE: "%p",
    RuleKind.UNIT_PRICE: "원/g",
}

RULES_NEEDING_THRESHOLD = {
    RuleKind.TARGET_PRICE,
    RuleKind.DISCOUNT_RATE,
    RuleKind.DISCOUNT_SPIKE,
    RuleKind.UNIT_PRICE,
}


def _fmt_price(v: Optional[float]) -> str:
    if v is None:
        return "-"
    try:
        return f"{int(round(v)):,}원"
    except (TypeError, ValueError):
        return "-"


def _default_rules() -> list[dict[str, Any]]:
    return [{"kind": k.value, "threshold": 0, "enabled": False} for k in RULE_KINDS_ORDER]


def _fit_to_screen(window: tk.Misc, want_w: int, want_h: int,
                   margin: int = 120) -> tuple[int, int]:
    """원하는 창 크기를 화면 안에 들어가도록 줄인다.

    상품명이 길어서 창을 꽤 넓게 잡아야 하는데(쿠팡 상품명은 660px를 넘기도 한다),
    해상도가 낮은 PC에서는 창이 화면 밖으로 나가버린다. 그래서 화면 크기에서
    여유분을 뺀 값으로 상한을 건다.
    """
    try:
        max_w = window.winfo_screenwidth() - margin
        max_h = window.winfo_screenheight() - margin
        return max(min(want_w, max_w), 900), max(min(want_h, max_h), 600)
    except Exception:
        return want_w, want_h


def _set_window_icon(window: tk.Misc) -> None:
    """창 제목표시줄/작업표시줄 아이콘을 지정한다.

    지정하지 않으면 Tk 기본 깃털 아이콘이 뜬다.

    두 가지를 **모두** 건다:
      * ``iconbitmap(default=...)`` — .ico 는 16~256px 멀티 해상도라 제목표시줄과
        작업표시줄 양쪽에서 또렷하다. ``default=`` 로 걸면 이후에 만들어지는
        Toplevel(상품 추가/편집 다이얼로그)에도 자동으로 적용된다.
      * ``iconphoto`` — 위쪽이 무시되는 경우를 대비한 보조 수단.
        PhotoImage는 참조가 사라지면 가비지 컬렉션되어 아이콘이 없어지므로
        창 객체에 붙여 살려 둔다.

    실패해도(아이콘 파일이 없거나 플랫폼이 지원하지 않아도) 창은 정상적으로 떠야 한다.
    """
    try:
        if ICON_ICO.exists():
            window.iconbitmap(default=str(ICON_ICO))
    except Exception:
        logger.debug("iconbitmap 설정 실패(무시)", exc_info=True)

    try:
        if ICON_FILE.exists():
            photo = tk.PhotoImage(master=window, file=str(ICON_FILE))
            window.iconphoto(True, photo)
            window._app_icon_ref = photo  # type: ignore[attr-defined]  # GC 방지
    except Exception:
        logger.debug("iconphoto 설정 실패(무시)", exc_info=True)


def _safe_handler(fn: Callable) -> Callable:
    """버튼 핸들러 공용 예외 안전 래퍼. 실패해도 창이 죽지 않게 한다."""

    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            logger.exception("설정 창 처리 중 오류(%s)", getattr(fn, "__name__", fn))
            try:
                messagebox.showerror(
                    "오류", "처리 중 오류가 발생했습니다. 로그를 확인해주세요.", parent=self.root
                )
            except Exception:
                logger.exception("오류 메시지박스 표시 실패")

    wrapper.__name__ = getattr(fn, "__name__", "handler")
    return wrapper


# ---------------------------------------------------------------------------
# 상품 추가 다이얼로그
# ---------------------------------------------------------------------------


class _AddProductDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, probe_fn: Optional[Callable[[str], dict]]):
        super().__init__(master)
        self.probe_fn = probe_fn
        self.result: Optional[dict[str, Any]] = None
        self.probed_info: Optional[dict[str, Any]] = None

        self.title("상품 추가")
        self.geometry("580x330")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        pad = {"padx": 12, "pady": 8}

        frm = ttk.Frame(self, padding=(4, 8))
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="쿠팡 상품 URL").grid(row=0, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(frm, textvariable=self.url_var, width=55)
        url_entry.grid(row=1, column=0, columnspan=2, sticky="we", padx=12)
        url_entry.focus_set()

        self.probe_btn = ttk.Button(frm, text="상품 조회", command=self._on_probe)
        self.probe_btn.grid(row=2, column=0, sticky="w", **pad)

        self.status_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=2, column=1, sticky="w", **pad
        )

        ttk.Label(frm, text="상품명").grid(row=3, column=0, sticky="w", **pad)
        self.name_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.name_var, width=55).grid(
            row=4, column=0, columnspan=2, sticky="we", padx=12
        )

        ttk.Label(frm, text="총 단백질 g (선택)").grid(row=5, column=0, sticky="w", **pad)
        self.protein_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.protein_var, width=20).grid(
            row=6, column=0, sticky="w", padx=12
        )

        btn_frm = ttk.Frame(self)
        btn_frm.pack(fill="x", pady=12, padx=4)
        ttk.Button(btn_frm, text="추가", command=self._on_add, style="Accent.TButton").pack(
            side="right", padx=12
        )
        ttk.Button(btn_frm, text="취소", command=self.destroy).pack(side="right")

        frm.columnconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

    def _on_probe(self) -> None:
        url = self.url_var.get().strip()
        parsed = parse_coupang_url(url)
        if parsed is None:
            messagebox.showerror("오류", "쿠팡 상품 URL이 아닙니다.", parent=self)
            return

        if self.probe_fn is None:
            self.status_var.set("자동 조회 기능 없음 — 상품명을 직접 입력하세요.")
            return

        self.status_var.set("확인 중...")
        self.probe_btn.config(state="disabled")

        def worker():
            info: Optional[dict] = None
            err: Optional[Exception] = None
            try:
                info = self.probe_fn(url)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                err = exc
                logger.exception("상품 조회(probe_fn) 실패: %s", url)
            try:
                self.after(0, lambda: self._on_probe_done(info, err))
            except tk.TclError:
                pass  # 창이 이미 닫힘

        threading.Thread(target=worker, daemon=True).start()

    def _on_probe_done(self, info: Optional[dict], err: Optional[Exception]) -> None:
        try:
            self.probe_btn.config(state="normal")
        except tk.TclError:
            return  # 창이 닫힘
        if err is not None or not info:
            self.status_var.set("조회 실패 — 상품명을 직접 입력하세요.")
            return
        self.status_var.set("조회 완료")
        self.probed_info = info
        if info.get("name"):
            self.name_var.set(str(info["name"]))

    def _on_add(self) -> None:
        url = self.url_var.get().strip()
        parsed = parse_coupang_url(url)
        if parsed is None:
            messagebox.showerror("오류", "쿠팡 상품 URL이 아닙니다.", parent=self)
            return

        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("오류", "상품명을 입력하세요.", parent=self)
            return

        protein_str = self.protein_var.get().strip()
        protein: Optional[float] = None
        if protein_str:
            try:
                protein = float(protein_str)
            except ValueError:
                messagebox.showerror("오류", "총 단백질 g은 숫자로 입력하세요.", parent=self)
                return

        info = self.probed_info or {}
        product_id = str(info.get("product_id") or parsed["product_id"])
        item_id = info.get("item_id") or parsed["item_id"]
        vendor_item_id = info.get("vendor_item_id") or parsed["vendor_item_id"]
        final_url = info.get("url") or url
        image_url = info.get("image_url")

        self.result = {
            "product_id": product_id,
            "url": final_url,
            "name": name,
            "item_id": item_id,
            "vendor_item_id": vendor_item_id,
            "protein_grams": protein,
            "enabled": True,
            "image_url": image_url,
            "rules": _default_rules(),
        }
        self.destroy()


# ---------------------------------------------------------------------------
# 상품 편집 다이얼로그
# ---------------------------------------------------------------------------


class _EditProductDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, entry: dict[str, Any]):
        super().__init__(master)
        self.entry = deepcopy(entry)
        self.result: Optional[dict[str, Any]] = None

        self.title(f"상품 편집 — {entry.get('name', '')}")
        self.geometry("580x580")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(self, padding=(4, 8))
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="상품명").grid(row=0, column=0, sticky="w", **pad)
        self.name_var = tk.StringVar(value=self.entry.get("name", ""))
        ttk.Entry(frm, textvariable=self.name_var, width=55).grid(
            row=1, column=0, columnspan=3, sticky="we", padx=10
        )

        ttk.Label(frm, text="URL (읽기전용)").grid(row=2, column=0, sticky="w", **pad)
        url_entry = ttk.Entry(frm, width=55)
        url_entry.insert(0, self.entry.get("url", ""))
        url_entry.config(state="readonly")
        url_entry.grid(row=3, column=0, columnspan=3, sticky="we", padx=10)

        ttk.Label(frm, text="총 단백질 g (선택, 단백질 단가 규칙용)").grid(
            row=4, column=0, sticky="w", **pad
        )
        protein_val = self.entry.get("protein_grams")
        self.protein_var = tk.StringVar(value="" if protein_val is None else str(protein_val))
        ttk.Entry(frm, textvariable=self.protein_var, width=20).grid(
            row=5, column=0, sticky="w", padx=10
        )

        self.enabled_var = tk.BooleanVar(value=bool(self.entry.get("enabled", True)))
        ttk.Checkbutton(frm, text="활성(감시함)", variable=self.enabled_var).grid(
            row=5, column=1, sticky="w", **pad
        )

        ttk.Separator(frm, orient="horizontal").grid(
            row=6, column=0, columnspan=3, sticky="we", pady=10
        )
        ttk.Label(frm, text="알림 규칙", style="SubHeader.TLabel").grid(
            row=7, column=0, sticky="w", **pad
        )

        rules_by_kind: dict[str, dict[str, Any]] = {
            r.get("kind"): r for r in self.entry.get("rules", [])
        }

        self.rule_vars: dict[RuleKind, dict[str, tk.Variable]] = {}
        row = 8
        for kind in RULE_KINDS_ORDER:
            saved = rules_by_kind.get(kind.value, {})
            enabled_var = tk.BooleanVar(value=bool(saved.get("enabled", False)))
            threshold_var = tk.StringVar(
                value="" if saved.get("threshold") in (None, 0) and kind not in RULES_NEEDING_THRESHOLD
                else str(saved.get("threshold", ""))
            )
            ttk.Checkbutton(
                frm, text=RULE_KIND_LABELS[kind], variable=enabled_var
            ).grid(row=row, column=0, sticky="w", padx=12, pady=5)
            if kind in RULES_NEEDING_THRESHOLD:
                ent = ttk.Entry(frm, textvariable=threshold_var, width=12)
                ent.grid(row=row, column=1, sticky="w", padx=5)
                ttk.Label(frm, text=RULE_KIND_UNITS.get(kind, "")).grid(
                    row=row, column=2, sticky="w"
                )
            else:
                ttk.Label(frm, text="(임계값 없음)", style="Muted.TLabel").grid(
                    row=row, column=1, sticky="w", padx=5
                )
            self.rule_vars[kind] = {"enabled": enabled_var, "threshold": threshold_var}
            row += 1

        btn_frm = ttk.Frame(self)
        btn_frm.pack(fill="x", pady=12, padx=4)
        ttk.Button(btn_frm, text="저장", command=self._on_save, style="Accent.TButton").pack(
            side="right", padx=12
        )
        ttk.Button(btn_frm, text="취소", command=self.destroy).pack(side="right")

        frm.columnconfigure(0, weight=1)

    def _on_save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("오류", "상품명을 입력하세요.", parent=self)
            return

        protein_str = self.protein_var.get().strip()
        protein: Optional[float] = None
        if protein_str:
            try:
                protein = float(protein_str)
            except ValueError:
                messagebox.showerror("오류", "총 단백질 g은 숫자로 입력하세요.", parent=self)
                return

        rules: list[dict[str, Any]] = []
        for kind in RULE_KINDS_ORDER:
            vars_ = self.rule_vars[kind]
            enabled = bool(vars_["enabled"].get())
            threshold: float = 0
            if kind in RULES_NEEDING_THRESHOLD:
                th_str = str(vars_["threshold"].get()).strip()
                if enabled:
                    if not th_str:
                        messagebox.showerror(
                            "오류",
                            f"'{RULE_KIND_LABELS[kind]}' 규칙의 임계값을 입력하세요.",
                            parent=self,
                        )
                        return
                    try:
                        threshold = float(th_str)
                    except ValueError:
                        messagebox.showerror(
                            "오류",
                            f"'{RULE_KIND_LABELS[kind]}' 임계값은 숫자로 입력하세요.",
                            parent=self,
                        )
                        return
                elif th_str:
                    try:
                        threshold = float(th_str)
                    except ValueError:
                        threshold = 0
            rules.append({"kind": kind.value, "threshold": threshold, "enabled": enabled})

        self.result = dict(self.entry)
        self.result.update(
            {
                "name": name,
                "protein_grams": protein,
                "enabled": bool(self.enabled_var.get()),
                "rules": rules,
            }
        )
        self.destroy()


# ---------------------------------------------------------------------------
# 메인 설정 창
# ---------------------------------------------------------------------------


class SettingsWindow:
    def __init__(
        self,
        store: Store,
        config_path: Optional[Path] = None,
        on_saved: Optional[Callable[[], None]] = None,
        probe_fn: Optional[Callable[[str], dict]] = None,
    ) -> None:
        self.store = store
        self.config_path = config_path or CONFIG_FILE
        self.on_saved = on_saved
        self.probe_fn = probe_fn

        self.cfg: AppConfig = load_config(self.config_path)

        self.root = tk.Tk()
        self.root.title("프로틴 할인 알림 — 설정")
        _set_window_icon(self.root)
        # 상품명이 길고(예: "EVLUTIONNUTRITION 웨이 프로틴 더블 리치 초콜릿 맛 - 복합 프로틴파우더")
        # 규칙 요약까지 한 줄에 들어가야 해서 가로를 넉넉히 잡는다.
        # 실측: 쿠팡 상품명은 660px를 넘기도 해서 가로를 넉넉히 잡아야 잘리지 않는다.
        win_w, win_h = _fit_to_screen(self.root, 1440, 800)
        self.root.minsize(1120, 660)
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        # 테마/글꼴 선택용 변수(그리고 즉시 반영을 위해 UI 구성 전에 미리 만들어 둔다).
        self.theme_var = tk.StringVar(value=self.cfg.theme)
        self.font_family_var = tk.StringVar(value=self.cfg.font_family)
        self.font_size_var = tk.StringVar(value=str(self.cfg.font_size))

        self._apply_theme(self.cfg.theme)
        self._apply_fonts(self.cfg.font_family, self.cfg.font_size)

        self._build_ui()
        self._refresh_tree()

    # -- 테마/글꼴 -------------------------------------------------------

    def _apply_theme(self, theme: str) -> None:
        """sv-ttk 테마를 즉시 적용한다. 창을 다시 열 필요가 없다."""
        if sv_ttk is not None:
            try:
                sv_ttk.set_theme(theme)
            except Exception:
                logger.exception("sv_ttk 테마 적용 실패: %s", theme)
        else:
            logger.warning("sv_ttk 미설치 — 기본 tkinter 테마로 표시됩니다.")

        # sv-ttk는 우리가 직접 색을 지정한 위젯(예: 안내 문구용 회색 라벨)까지
        # 자동으로 바꿔주지 않으므로, 테마별 보조 텍스트 색을 별도 스타일로 관리한다.
        # ttk.Treeview 자체는 sv-ttk가 제공하는 스타일을 그대로 쓰므로(직접 지정한
        # 태그 색이 없음) 다크 테마에서도 배경/글자색이 자동으로 맞춰진다.
        style = ttk.Style()
        muted_fg = "#9a9a9a" if theme == "dark" else "#666666"
        style.configure("Muted.TLabel", foreground=muted_fg)

    def _apply_fonts(self, family: str, size: int) -> None:
        """Tk 명명 글꼴 + sv-ttk 명명 글꼴 + ttk 스타일에 글꼴을 일괄 반영한다."""
        family = resolve_font_family(family, getattr(self, "root", None))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=size)
            except tk.TclError:
                continue
        # TkFixedFont는 고정폭 글꼴 정체성을 유지해야 하므로 크기만 맞춘다.
        try:
            tkfont.nametofont("TkFixedFont").configure(size=size)
        except tk.TclError:
            pass

        # sv-ttk 명명 글꼴 덮어쓰기 — 이게 없으면 표/버튼/체크박스가 굴림으로 나온다.
        for fname, delta, bold in SV_TTK_FONTS:
            try:
                tkfont.nametofont(fname).configure(
                    family=family,
                    size=max(size + delta, 7),
                    weight="bold" if bold else "normal",
                )
            except tk.TclError:
                continue  # 테마가 아직 안 올라왔거나 해당 글꼴이 없는 경우

        style = ttk.Style()
        # 테마가 스타일에 직접 박아둔 글꼴도 덮어쓴다(이중 안전장치).
        for style_name in TTK_FONT_STYLES:
            try:
                style.configure(style_name, font=(family, size))
            except tk.TclError:
                continue
        # Treeview 행 높이는 글꼴을 따라가지 않으므로 직접 계산해 준다.
        style.configure("Treeview", rowheight=max(24, int(size * 2.4)))
        style.configure("Treeview.Heading", font=(family, size, "bold"))
        style.configure("TLabelframe.Label", font=(family, size, "bold"))
        # 시각적 위계: 제목/부제목은 조금 더 크고 굵게.
        style.configure("Header.TLabel", font=(family, size + 6, "bold"))
        style.configure("SubHeader.TLabel", font=(family, size + 1, "bold"))
        style.configure("Muted.TLabel", font=(family, size))

    @_safe_handler
    def _on_theme_change(self) -> None:
        theme = self.theme_var.get()
        if theme not in ("light", "dark"):
            theme = "light"
        self._apply_theme(theme)
        # 테마를 다시 입히면 sv-ttk가 자기 글꼴(Segoe UI Variable — 한글 없음)로
        # 되돌려 버린다. 그래서 테마 적용 직후 글꼴을 반드시 다시 씌운다.
        self._apply_fonts(self.cfg.font_family, self.cfg.font_size)
        self.cfg.theme = theme
        self._save_config_to_disk()
        logger.info("화면 테마 변경 및 저장됨: %s", theme)

    @_safe_handler
    def _on_font_change(self, _event: Any = None) -> None:
        family = self.font_family_var.get().strip() or self.cfg.font_family
        try:
            size = int(self.font_size_var.get())
        except ValueError:
            size = self.cfg.font_size
        self._apply_fonts(family, size)
        self.cfg.font_family = family
        self.cfg.font_size = size
        self._save_config_to_disk()
        logger.info("글꼴 변경 및 저장됨: %s %spt", family, size)

    # -- 공개 API -----------------------------------------------------

    def show(self) -> None:
        """창을 화면에 보이고 앞으로 가져온다. mainloop는 호출하지 않는다."""
        try:
            self.root.deiconify()
        except tk.TclError:
            return
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        try:
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

    # -- UI 구성 --------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root

        # 최상단: 창 제목/설명 — 시각적 위계를 위한 헤더.
        header_frm = ttk.Frame(root)
        header_frm.pack(fill="x", padx=16, pady=(16, 6))
        ttk.Label(header_frm, text="프로틴 할인 알림 설정", style="Header.TLabel").pack(
            side="left"
        )
        ttk.Label(
            header_frm,
            text="감시 중인 상품과 알림 규칙을 관리합니다.",
            style="Muted.TLabel",
        ).pack(side="left", padx=(12, 0))

        # 상단: 감시 상품 목록
        top_frm = ttk.Frame(root)
        top_frm.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        columns = ("name", "current", "min", "target", "enabled", "rules")
        headers = {
            "name": "상품명",
            "current": "현재가",
            "min": "최저가",
            "target": "목표가",
            "enabled": "활성",
            "rules": "규칙요약",
        }
        # 상품명과 규칙 요약이 가장 길다. 나머지 금액 열은 "100,950원" 이 여유롭게 들어갈 폭.
        # 실측: 쿠팡 상품명은 길면 578px, 규칙 요약은 보통 220px(전 규칙 활성 시 370px).
        # 상품명이 잘리지 않도록 600px를 주고, 남는 공간은 상품명/규칙요약이 나눠 갖는다.
        widths = {"name": 690, "current": 105, "min": 105, "target": 105, "enabled": 60, "rules": 310}
        minwidths = {"name": 240, "current": 80, "min": 80, "target": 80, "enabled": 50, "rules": 160}
        # 창을 넓히면 상품명/규칙요약만 늘어나도록 금액 열은 고정한다.
        stretches = {"name": True, "rules": True}
        anchors = {"name": "w", "rules": "w"}

        self.tree = ttk.Treeview(top_frm, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self.tree.heading(col, text=headers[col])
            self.tree.column(
                col,
                width=widths[col],
                minwidth=minwidths[col],
                stretch=stretches.get(col, False),
                anchor=anchors.get(col, "center"),
            )
        vsb = ttk.Scrollbar(top_frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_edit)

        # 중단: 버튼 바
        btn_frm = ttk.Frame(root)
        btn_frm.pack(fill="x", padx=16, pady=8)
        ttk.Button(
            btn_frm, text="상품 추가", command=self._on_add_product, style="Accent.TButton"
        ).pack(side="left")
        ttk.Button(btn_frm, text="편집", command=self._on_edit).pack(side="left", padx=8)
        ttk.Button(btn_frm, text="삭제", command=self._on_delete).pack(side="left")
        ttk.Button(btn_frm, text="가격 이력", command=self._on_show_history).pack(
            side="left", padx=8
        )

        # 하단: 전역 설정
        cfg_frm = ttk.LabelFrame(root, text="전역 설정")
        cfg_frm.pack(fill="x", padx=16, pady=10)

        pad = {"padx": 10, "pady": 8}

        ttk.Label(cfg_frm, text="확인 주기(분)").grid(row=0, column=0, sticky="w", **pad)
        self.poll_var = tk.StringVar(value=str(self.cfg.poll_interval_minutes))
        ttk.Entry(cfg_frm, textvariable=self.poll_var, width=8).grid(row=0, column=1, sticky="w")

        ttk.Label(cfg_frm, text="지터(분)").grid(row=0, column=2, sticky="w", **pad)
        self.jitter_var = tk.StringVar(value=str(self.cfg.jitter_minutes))
        ttk.Entry(cfg_frm, textvariable=self.jitter_var, width=8).grid(row=0, column=3, sticky="w")

        ttk.Label(cfg_frm, text="쿨다운(시간)").grid(row=0, column=4, sticky="w", **pad)
        self.cooldown_var = tk.StringVar(value=str(self.cfg.cooldown_hours))
        ttk.Entry(cfg_frm, textvariable=self.cooldown_var, width=8).grid(row=0, column=5, sticky="w")

        self.renotify_var = tk.BooleanVar(value=self.cfg.renotify_on_lower)
        ttk.Checkbutton(cfg_frm, text="더 싸지면 재알림", variable=self.renotify_var).grid(
            row=1, column=0, columnspan=2, sticky="w", **pad
        )

        self.sound_var = tk.BooleanVar(value=self.cfg.sound)
        ttk.Checkbutton(cfg_frm, text="알림음", variable=self.sound_var).grid(
            row=1, column=2, columnspan=2, sticky="w", **pad
        )

        self.headless_var = tk.BooleanVar(value=self.cfg.headless)
        # 진짜 헤드리스는 쿠팡이 차단하므로, 창을 화면 밖으로 보내는 방식이다.
        ttk.Checkbutton(cfg_frm, text="확인할 때 브라우저 창 숨기기 (권장)", variable=self.headless_var).grid(
            row=1, column=4, columnspan=2, sticky="w", **pad
        )

        ttk.Separator(cfg_frm, orient="horizontal").grid(
            row=2, column=0, columnspan=6, sticky="we", padx=10, pady=(4, 2)
        )

        # 화면 테마 — 선택 즉시 반영되고 config.json에 저장된다.
        ttk.Label(cfg_frm, text="화면 테마").grid(row=3, column=0, sticky="w", **pad)
        theme_frm = ttk.Frame(cfg_frm)
        theme_frm.grid(row=3, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(
            theme_frm, text="라이트", value="light", variable=self.theme_var,
            command=self._on_theme_change,
        ).pack(side="left")
        ttk.Radiobutton(
            theme_frm, text="다크", value="dark", variable=self.theme_var,
            command=self._on_theme_change,
        ).pack(side="left", padx=(12, 0))

        # 글꼴 — 선택 즉시 반영되고 config.json에 저장된다.
        ttk.Label(cfg_frm, text="글꼴").grid(row=3, column=3, sticky="w", **pad)
        font_frm = ttk.Frame(cfg_frm)
        font_frm.grid(row=3, column=4, columnspan=2, sticky="w")
        family_combo = ttk.Combobox(
            font_frm,
            textvariable=self.font_family_var,
            values=installed_font_families(self.root),
            width=20,
            state="readonly",
        )
        family_combo.pack(side="left")
        family_combo.bind("<<ComboboxSelected>>", self._on_font_change)
        size_combo = ttk.Combobox(
            font_frm,
            textvariable=self.font_size_var,
            values=[str(s) for s in AVAILABLE_FONT_SIZES],
            width=4,
            state="readonly",
        )
        size_combo.pack(side="left", padx=(6, 0))
        size_combo.bind("<<ComboboxSelected>>", self._on_font_change)

        for col in range(6):
            cfg_frm.columnconfigure(col, weight=0)

        bottom_btn_frm = ttk.Frame(root)
        bottom_btn_frm.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(
            bottom_btn_frm, text="저장", command=self._on_save_global, style="Accent.TButton"
        ).pack(side="right")
        ttk.Button(bottom_btn_frm, text="닫기", command=self.root.destroy).pack(
            side="right", padx=8
        )

    # -- 데이터 헬퍼 ------------------------------------------------------

    def _find_product(self, product_id: str) -> Optional[dict[str, Any]]:
        for p in self.cfg.products:
            if p.get("product_id") == product_id:
                return p
        return None

    def _find_product_index(self, product_id: str) -> int:
        for i, p in enumerate(self.cfg.products):
            if p.get("product_id") == product_id:
                return i
        return -1

    def _rule_summary(self, rules: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for r in rules:
            if not r.get("enabled"):
                continue
            try:
                kind = RuleKind(r.get("kind"))
            except ValueError:
                continue
            label = RULE_KIND_LABELS.get(kind, str(r.get("kind")))
            if kind in RULES_NEEDING_THRESHOLD:
                th = r.get("threshold", 0) or 0
                unit = RULE_KIND_UNITS.get(kind, "")
                if kind == RuleKind.TARGET_PRICE:
                    parts.append(f"{label} {int(th):,}{unit}")
                else:
                    parts.append(f"{label} {th:g}{unit}")
            else:
                parts.append(label)
        return ", ".join(parts) if parts else "-"

    def _refresh_tree(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for entry in self.cfg.products:
            pid = entry.get("product_id", "")
            name = entry.get("name", "")
            latest = None
            minp = None
            try:
                latest = self.store.latest_price(pid)
            except Exception:
                logger.exception("latest_price 조회 실패: %s", pid)
            try:
                minp = self.store.min_price(pid)
            except Exception:
                logger.exception("min_price 조회 실패: %s", pid)

            current = latest.price if latest is not None else None
            rules = entry.get("rules", [])
            target_rule = next(
                (r for r in rules if r.get("kind") == RuleKind.TARGET_PRICE.value and r.get("enabled")),
                None,
            )
            target = target_rule.get("threshold") if target_rule else None
            enabled_str = "예" if entry.get("enabled", True) else "아니오"

            self.tree.insert(
                "",
                "end",
                iid=pid,
                values=(
                    name,
                    _fmt_price(current),
                    _fmt_price(minp),
                    _fmt_price(target) if target is not None else "-",
                    enabled_str,
                    self._rule_summary(rules),
                ),
            )

    def _sync_store_product(self, entry: dict[str, Any]) -> None:
        product = product_of(entry)
        try:
            existing = self.store.get_product(product.product_id)
        except Exception:
            existing = None
            logger.exception("get_product 조회 실패: %s", product.product_id)
        if existing is not None and existing.added_at is not None:
            product.added_at = existing.added_at
        else:
            product.added_at = datetime.now()
        self.store.upsert_product(product)

    def _save_config_to_disk(self) -> None:
        try:
            save_config(self.cfg, self.config_path)
        except Exception:
            logger.exception("config.json 저장 실패: %s", self.config_path)
            messagebox.showerror(
                "오류", "설정 파일 저장에 실패했습니다. 로그를 확인해주세요.", parent=self.root
            )
            raise

    def _selected_product_id(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    # -- 버튼 핸들러 ------------------------------------------------------

    @_safe_handler
    def _on_add_product(self) -> None:
        dlg = _AddProductDialog(self.root, self.probe_fn)
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        entry = dlg.result
        if self._find_product(entry["product_id"]) is not None:
            messagebox.showerror("오류", "이미 등록된 상품입니다.", parent=self.root)
            return

        self.cfg.products.append(entry)
        self._sync_store_product(entry)
        self._save_config_to_disk()
        self._refresh_tree()
        logger.info("상품 추가됨: %s (%s)", entry["name"], entry["product_id"])

        # 규칙 설정을 바로 이어서 할 수 있도록 편집 다이얼로그를 연다.
        self._open_edit_dialog(entry["product_id"])

    @_safe_handler
    def _on_edit(self, _event: Any = None) -> None:
        pid = self._selected_product_id()
        if pid is None:
            messagebox.showinfo("안내", "편집할 상품을 선택하세요.", parent=self.root)
            return
        self._open_edit_dialog(pid)

    def _open_edit_dialog(self, product_id: str) -> None:
        entry = self._find_product(product_id)
        if entry is None:
            return
        dlg = _EditProductDialog(self.root, entry)
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        idx = self._find_product_index(product_id)
        if idx < 0:
            return
        self.cfg.products[idx] = dlg.result
        self._sync_store_product(dlg.result)
        self._save_config_to_disk()
        self._refresh_tree()
        logger.info("상품 편집됨: %s (%s)", dlg.result.get("name"), product_id)

    @_safe_handler
    def _on_delete(self) -> None:
        pid = self._selected_product_id()
        if pid is None:
            messagebox.showinfo("안내", "삭제할 상품을 선택하세요.", parent=self.root)
            return
        entry = self._find_product(pid)
        if entry is None:
            return
        if not messagebox.askyesno(
            "삭제 확인", f"'{entry.get('name', pid)}' 상품을 삭제하시겠습니까?", parent=self.root
        ):
            return
        self.cfg.products = [p for p in self.cfg.products if p.get("product_id") != pid]
        try:
            self.store.delete_product(pid)
        except Exception:
            logger.exception("store.delete_product 실패: %s", pid)
        self._save_config_to_disk()
        self._refresh_tree()
        logger.info("상품 삭제됨: %s", pid)

    @_safe_handler
    def _on_show_history(self) -> None:
        pid = self._selected_product_id()
        if pid is None:
            messagebox.showinfo("안내", "가격 이력을 볼 상품을 선택하세요.", parent=self.root)
            return
        entry = self._find_product(pid)
        name = entry.get("name", pid) if entry else pid

        history = self.store.price_history(pid, days=90)

        win = tk.Toplevel(self.root)
        win.title(f"가격 이력 — {name}")
        # 열이 7개나 되고 "2026-09-07 09:11" 같은 값이 들어가서 넉넉해야 한다.
        hist_w, hist_h = _fit_to_screen(win, 980, 660)
        win.geometry(f"{hist_w}x{hist_h}")
        win.minsize(820, 420)
        win.transient(self.root)

        columns = (
            "checked_at",
            "price",
            "list_price",
            "discount_rate",
            "coupon_price",
            "wow_price",
            "availability",
        )
        headers = {
            "checked_at": "확인 시각",
            "price": "실구매가",
            "list_price": "정가",
            "discount_rate": "할인율",
            "coupon_price": "쿠폰가",
            "wow_price": "와우가",
            "availability": "재고",
        }
        # 열마다 담기는 값의 길이가 다르다. 확인 시각이 가장 길고, 할인율이 가장 짧다.
        hist_widths = {
            "checked_at": 165, "price": 115, "list_price": 115, "discount_rate": 85,
            "coupon_price": 115, "wow_price": 115, "availability": 100,
        }
        tv = ttk.Treeview(win, columns=columns, show="headings")
        for c in columns:
            tv.heading(c, text=headers[c])
            tv.column(
                c,
                width=hist_widths[c],
                minwidth=max(hist_widths[c] - 30, 60),
                stretch=(c == "checked_at"),
                anchor="center",
            )
        vsb = ttk.Scrollbar(win, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        avail_labels = {"in_stock": "재고있음", "out_of_stock": "품절", "unknown": "알수없음"}
        for pp in reversed(history):  # 최신 순
            tv.insert(
                "",
                "end",
                values=(
                    pp.checked_at.strftime("%Y-%m-%d %H:%M"),
                    _fmt_price(pp.price),
                    _fmt_price(pp.list_price),
                    f"{pp.discount_rate}%" if pp.discount_rate is not None else "-",
                    _fmt_price(pp.coupon_price),
                    _fmt_price(pp.wow_price),
                    avail_labels.get(pp.availability.value, str(pp.availability.value)),
                ),
            )
        if not history:
            messagebox.showinfo("안내", "아직 수집된 가격 이력이 없습니다.", parent=win)

    @_safe_handler
    def _on_save_global(self) -> None:
        try:
            poll = int(self.poll_var.get().strip())
            jitter = int(self.jitter_var.get().strip())
            cooldown = int(self.cooldown_var.get().strip())
        except ValueError:
            messagebox.showerror(
                "오류", "확인 주기 / 지터 / 쿨다운은 정수로 입력하세요.", parent=self.root
            )
            return
        if poll <= 0:
            messagebox.showerror("오류", "확인 주기는 1분 이상이어야 합니다.", parent=self.root)
            return
        if jitter < 0 or cooldown < 0:
            messagebox.showerror("오류", "지터/쿨다운은 0 이상이어야 합니다.", parent=self.root)
            return

        self.cfg.poll_interval_minutes = poll
        self.cfg.jitter_minutes = jitter
        self.cfg.cooldown_hours = cooldown
        self.cfg.renotify_on_lower = bool(self.renotify_var.get())
        self.cfg.sound = bool(self.sound_var.get())
        self.cfg.headless = bool(self.headless_var.get())

        self._save_config_to_disk()
        logger.info("전역 설정 저장됨: %s", self.config_path)
        messagebox.showinfo("저장 완료", "설정이 저장되었습니다.", parent=self.root)

        if self.on_saved is not None:
            try:
                self.on_saved()
            except Exception:
                logger.exception("on_saved 콜백 실행 중 오류")


# ---------------------------------------------------------------------------
# 싱글턴 진입점 — 트레이 스레드 등 임의 스레드에서 안전하게 호출 가능
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_singleton_window: Optional[SettingsWindow] = None


def open_settings(
    store: Store,
    config_path: Optional[Path] = None,
    on_saved: Optional[Callable[[], None]] = None,
    probe_fn: Optional[Callable[[str], dict]] = None,
) -> None:
    """설정 창을 연다(편의 함수). 이미 열려 있으면 기존 창을 앞으로 가져온다.

    tkinter는 생성 스레드에서만 안전하므로, 새 창이 필요한 경우 전용 스레드를
    만들어 그 안에서 Tk() 생성과 mainloop 실행을 모두 수행한다. 이 함수 자체는
    즉시 반환하며(non-blocking) 호출한 스레드(예: 트레이 아이콘 스레드)를
    막지 않는다.
    """
    global _singleton_window

    with _singleton_lock:
        win = _singleton_window
        if win is not None:
            alive = False
            try:
                alive = bool(win.root.winfo_exists())
            except Exception:
                alive = False
            if alive:
                try:
                    win.root.after(0, win.show)
                except Exception:
                    logger.exception("기존 설정 창을 앞으로 가져오기 실패")
                return
            _singleton_window = None

        def _runner() -> None:
            global _singleton_window
            window: Optional[SettingsWindow] = None
            try:
                window = SettingsWindow(
                    store, config_path=config_path, on_saved=on_saved, probe_fn=probe_fn
                )
                with _singleton_lock:
                    _singleton_window = window
                window.show()
                window.root.mainloop()
            except Exception:
                logger.exception("설정 창 스레드 실행 중 오류")
            finally:
                with _singleton_lock:
                    if _singleton_window is window:
                        _singleton_window = None

        threading.Thread(target=_runner, name="SettingsWindowThread", daemon=True).start()


if __name__ == "__main__":
    import shutil
    import tempfile

    logging.basicConfig(level=logging.INFO)

    # 1) URL 파싱 자체 테스트 (GUI 불필요)
    cases = [
        "https://www.coupang.com/vp/products/1234567890?itemId=111&vendorItemId=222",
        "https://www.coupang.com/vp/products/9988776655",
        "https://m.coupang.com/vm/products/9988776655/vp/products/5566778899?itemId=333&vendorItemId=444",
        "https://www.coupang.com/np/search?q=protein",  # /vp/products/ 없음 -> None
    ]
    r0 = parse_coupang_url(cases[0])
    assert r0 == {"product_id": "1234567890", "item_id": "111", "vendor_item_id": "222"}, r0
    r1 = parse_coupang_url(cases[1])
    assert r1 == {"product_id": "9988776655", "item_id": None, "vendor_item_id": None}, r1
    r2 = parse_coupang_url(cases[2])
    assert r2 is not None and r2["product_id"] == "5566778899"
    r3 = parse_coupang_url(cases[3])
    assert r3 is None, r3
    r4 = parse_coupang_url("")
    assert r4 is None

    print("OK: parse_coupang_url self-test passed")

    # 2) 임시 DB/Config로 SettingsWindow를 잠깐 띄워 본 뒤 자동 종료
    tmp_dir = Path(tempfile.mkdtemp(prefix="protein_settings_gui_test_"))
    try:
        from core.models import Availability, PricePoint, Product

        db_path = tmp_dir / "test.db"
        cfg_path = tmp_dir / "config.json"

        store = Store(db_path)
        p1 = Product(
            product_id="1111",
            url="https://www.coupang.com/vp/products/1111?itemId=1&vendorItemId=2",
            name="테스트 프로틴 A",
            protein_grams=800.0,
            added_at=datetime.now(),
        )
        store.upsert_product(p1)
        store.add_price(
            PricePoint(
                product_id="1111",
                checked_at=datetime.now(),
                price=39900,
                list_price=52900,
                discount_rate=25,
                coupon_price=None,
                wow_price=None,
                availability=Availability.IN_STOCK,
            )
        )

        cfg = load_config(cfg_path)
        cfg.products.append(
            {
                "product_id": "1111",
                "url": p1.url,
                "name": p1.name,
                "protein_grams": 800.0,
                "enabled": True,
                "rules": [
                    {"kind": "target_price", "threshold": 45000, "enabled": True},
                    {"kind": "lowest_ever", "threshold": 0, "enabled": True},
                ],
            }
        )
        save_config(cfg, cfg_path)

        win = SettingsWindow(store, config_path=cfg_path)
        win.show()
        win.root.after(2000, win.root.destroy)  # 2초 후 자동 종료(무한 대기 방지)
        win.root.mainloop()
        store.close()
        print("OK: SettingsWindow GUI smoke test passed")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
