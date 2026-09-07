"""앱 아이콘 생성기 (Pillow 도형 그리기만 사용, 외부 이미지 다운로드 없음).

## 디자인 방향

운동(덤벨)을 한눈에 알아보게 하되, 장식은 최대한 걷어낸 **플랫 + 그라디언트**
스타일이다. 요즘 앱 아이콘의 기본 문법을 따랐다:

* **스퀘어클(둥근 사각형)** 바탕 — 모서리 반경을 변 길이의 약 22%로 잡아
  iOS/Windows 11 계열 아이콘과 비슷한 인상을 준다.
* **대각선 그라디언트** (인디고 → 바이올렛) — 단색보다 깊이가 생기고,
  광택·그림자 같은 옛날식 장식 없이도 고급스러워 보인다.
* **순백 덤벨 실루엣 하나만** — 배지나 화살표 같은 부가 요소를 넣으면
  16px 트레이 아이콘에서 뭉개져서 오히려 지저분해진다. 그래서 뺐다.

## 작은 크기에서도 읽히게 하는 장치

트레이 아이콘은 실제로 16~24px로 그려진다. 그래서
* 덤벨이 캔버스 폭의 **약 63%**를 차지하도록 크게 잡고,
* 원판을 안쪽 2개 + 바깥쪽 2개로 단순화하고(더 나누면 뭉갬),
* 전체를 잇는 **가는 축(rod)** 을 뒤에 깔아 원판들이 끊겨 보이지 않게 했다.

모든 도형은 1024px 캔버스에 그린 뒤 LANCZOS로 축소한다(안티앨리어싱).

## 만들어지는 파일

* ``ui/icon.png``      256x256 — 토스트 알림용
* ``ui/icon_tray.png`` 64x64   — 트레이 아이콘용
* ``ui/icon.ico``      16~256 멀티 해상도 — 설정 창 제목표시줄용

실행: ``python ui/make_icon.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent

# 작업 캔버스. 크게 그린 뒤 줄여야 곡선이 깨끗하다.
CANVAS = 1024

# 바탕 그라디언트 (좌상단 → 우하단). 인디고에서 바이올렛으로.
GRADIENT_START = (79, 70, 229)     # #4F46E5
GRADIENT_END = (147, 51, 234)      # #9333EA

GLYPH = (255, 255, 255, 255)       # 덤벨: 순백

CORNER_RADIUS_RATIO = 0.2237       # 스퀘어클 느낌의 모서리 반경


def _diagonal_gradient(size: int, start: tuple[int, int, int],
                       end: tuple[int, int, int]) -> Image.Image:
    """좌상단에서 우하단으로 흐르는 선형 그라디언트를 만든다."""
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    denom = max((size - 1) * 2, 1)
    for y in range(size):
        for x in range(size):
            # 대각선 진행도 0.0(좌상) ~ 1.0(우하)
            t = (x + y) / denom
            px[x, y] = (
                round(start[0] + (end[0] - start[0]) * t),
                round(start[1] + (end[1] - start[1]) * t),
                round(start[2] + (end[2] - start[2]) * t),
            )
    return grad


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _draw_dumbbell(draw: ImageDraw.ImageDraw, size: int) -> None:
    """중앙에 덤벨을 그린다. 좌표는 1024 기준 비율로 계산한다.

    원판을 여러 겹으로 나누면 16px 트레이 아이콘에서 서로 뭉개져 한 덩어리로
    보인다(실제로 그렇게 나왔다). 그래서 **원판은 한 쌍만** 두고, 바가 원판
    바깥으로 살짝 튀어나오게 해서 덤벨 특유의 실루엣을 만든다. 요소가 적을수록
    작은 크기에서 또렷하고, 큰 크기에서도 더 정돈돼 보인다.
    """
    s = size / CANVAS          # 배율
    c = size / 2               # 중심

    def rr(x0: float, y0: float, x1: float, y1: float, r: float) -> None:
        draw.rounded_rectangle((x0, y0, x1, y1), radius=r * s, fill=GLYPH)

    # 1) 바 — 원판 바깥으로 조금 튀어나와 손잡이 끝을 만든다.
    rr(c - 330 * s, c - 30 * s, c + 330 * s, c + 30 * s, 30)

    # 2) 원판 한 쌍
    for sign in (-1, 1):
        cx = c + sign * 232 * s
        rr(cx - 60 * s, c - 178 * s, cx + 60 * s, c + 178 * s, 44)


def build_icon(size: int = CANVAS) -> Image.Image:
    """지정 크기의 아이콘 이미지를 만든다(항상 1024로 그린 뒤 축소)."""
    radius = int(CANVAS * CORNER_RADIUS_RATIO)

    base = _diagonal_gradient(CANVAS, GRADIENT_START, GRADIENT_END).convert("RGBA")
    base.putalpha(_rounded_mask(CANVAS, radius))

    glyph_layer = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    _draw_dumbbell(ImageDraw.Draw(glyph_layer), CANVAS)

    icon = Image.alpha_composite(base, glyph_layer)
    if size != CANVAS:
        icon = icon.resize((size, size), Image.LANCZOS)
    return icon


def main() -> int:
    out_png = HERE / "icon.png"
    out_tray = HERE / "icon_tray.png"
    out_ico = HERE / "icon.ico"

    master = build_icon(CANVAS)

    master.resize((256, 256), Image.LANCZOS).save(out_png)
    master.resize((64, 64), Image.LANCZOS).save(out_tray)
    # 멀티 해상도 ico — 제목표시줄(16)부터 작업표시줄(32/48)까지 또렷하게.
    master.resize((256, 256), Image.LANCZOS).save(
        out_ico, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )

    for p in (out_png, out_tray, out_ico):
        with Image.open(p) as im:
            print(f"  생성: {p.name:14s} {im.size}  {p.stat().st_size:,} bytes")
    print("\nOK: 아이콘 생성 완료")
    return 0


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)
    print("아이콘을 만듭니다 (덤벨 · 인디고→바이올렛 그라디언트 스퀘어클)")
    sys.exit(main())
