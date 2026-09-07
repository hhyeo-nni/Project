# 프로틴 할인 알림 — 인터페이스 명세서 (v1)

> 모든 모듈은 이 문서의 시그니처를 **정확히** 지킬 것. 여기 정의되지 않은 공용 타입을 새로 만들지 말 것.

## 0. 환경

- Python 3.14 / Windows 11
- 설치 완료: `selenium`, `seleniumbase`, `pystray`, `Pillow`, `sv-ttk`(`ui/settings_gui.py` 설정 창 UI 테마 — Windows 11 스타일, 6절 `theme`/`font_family`/`font_size` 참고), `winotify`(진단 도구 `tools/알림테스트.py` 전용 — `core/notifier.py`는 더 이상 쓰지 않음, 5-1절 참고)
- 표준 라이브러리 우선. 인코딩은 항상 `utf-8` 명시.
- 프로젝트 루트: `D:\김호현\AI\Project\프로틴 할인 알림`
- 모든 경로는 `core/paths.py`의 상수를 통해서만 접근한다.

## 1. 공용 타입 — `core/models.py` (단일 진실 공급원)

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

class Availability(str, Enum):
    IN_STOCK   = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN    = "unknown"

class RuleKind(str, Enum):
    TARGET_PRICE   = "target_price"     # A. 목표가 이하
    DISCOUNT_RATE  = "discount_rate"    # B. 할인율 N% 이상
    DISCOUNT_SPIKE = "discount_spike"   # B'. 최근 평균 대비 +N%p 급등
    LOWEST_EVER    = "lowest_ever"      # C. 최저가 갱신
    RESTOCK        = "restock"          # D. 재입고
    UNIT_PRICE     = "unit_price"       # E. 단백질 1g당 단가 이하

@dataclass
class Product:
    product_id: str                 # 쿠팡 productId (URL의 /vp/products/{id})
    url: str                        # 정규화된 상품 URL (itemId/vendorItemId 포함)
    name: str
    item_id: Optional[str] = None
    vendor_item_id: Optional[str] = None
    protein_grams: Optional[float] = None   # 총 단백질 g (UNIT_PRICE 규칙용, 사용자 입력)
    enabled: bool = True
    added_at: Optional[datetime] = None
    image_url: Optional[str] = None

@dataclass
class PricePoint:
    product_id: str
    checked_at: datetime
    price: Optional[int]            # 최종 실구매가(원). 쿠폰가 있으면 쿠폰가. 파싱 실패 시 None
    list_price: Optional[int]       # 정가(할인 전). 없으면 None
    discount_rate: Optional[int]    # 할인율 % (정수). 없으면 None
    coupon_price: Optional[int]     # 쿠폰 적용가. 없으면 None
    wow_price: Optional[int]        # 와우회원가. 없으면 None
    availability: Availability = Availability.UNKNOWN
    raw_note: str = ""              # 파싱 디버그용 메모

@dataclass
class AlertRule:
    kind: RuleKind
    threshold: float                # TARGET_PRICE:원 / DISCOUNT_*:% / UNIT_PRICE:원per g / RESTOCK,LOWEST_EVER: 무시(0)
    enabled: bool = True

@dataclass
class AlertEvent:
    product_id: str
    product_name: str
    kind: RuleKind
    fired_at: datetime
    price: int
    prev_price: Optional[int]
    message: str                    # 토스트 본문 (핵심 한 줄)
    url: str
    detail: str = ""                # 토스트 셋째 줄 (정가·할인율·직전가 등 부연 설명, core/rules.py의 _build_detail()이 생성)
```

## 2. 경로 — `core/paths.py`

```python
ROOT: Path            # 프로젝트 루트
CONFIG_FILE: Path     # ROOT/config.json
SELECTORS_FILE: Path  # ROOT/selectors.json
DATA_DIR: Path        # ROOT/data
DB_FILE: Path         # ROOT/data/protein.db
LOG_DIR: Path         # ROOT/data/logs
ICON_FILE: Path       # ROOT/ui/icon.png  (토스트/트레이)
ICON_ICO: Path        # ROOT/ui/icon.ico  (창 제목표시줄, 16~256 멀티 해상도)
def ensure_dirs() -> None
```

## 3. 저장소 — `core/store.py`

SQLite (`sqlite3`, `check_same_thread=False`, WAL 모드). 스키마:

- `products(product_id TEXT PK, url, name, item_id, vendor_item_id, protein_grams REAL, enabled INT, added_at TEXT, image_url TEXT)`
- `price_history(id INTEGER PK AUTOINCREMENT, product_id TEXT, checked_at TEXT, price INT, list_price INT, discount_rate INT, coupon_price INT, wow_price INT, availability TEXT, raw_note TEXT)` + `INDEX(product_id, checked_at)`
- `alert_history(id INTEGER PK AUTOINCREMENT, product_id TEXT, kind TEXT, fired_at TEXT, price INT, message TEXT)` + `INDEX(product_id, kind, fired_at)`

```python
class Store:
    def __init__(self, db_path: Path = DB_FILE) -> None       # 스키마 자동 생성(멱등)
    def close(self) -> None

    # products
    def upsert_product(self, p: Product) -> None
    def get_product(self, product_id: str) -> Optional[Product]
    def list_products(self, enabled_only: bool = False) -> list[Product]
    def delete_product(self, product_id: str) -> None

    # price history
    def add_price(self, pp: PricePoint) -> None
    def latest_price(self, product_id: str) -> Optional[PricePoint]
    def previous_price(self, product_id: str) -> Optional[PricePoint]   # 최신 바로 직전
    def min_price(self, product_id: str, days: Optional[int] = None) -> Optional[int]  # days=None이면 전체 기간
    def avg_discount_rate(self, product_id: str, days: int = 7) -> Optional[float]
    def price_history(self, product_id: str, days: int = 90) -> list[PricePoint]

    # alert history
    def add_alert(self, ev: AlertEvent) -> None
    def last_alert(self, product_id: str, kind: RuleKind) -> Optional[AlertEvent]
```

**규칙**: 모든 datetime은 DB에 ISO8601 문자열(`dt.isoformat()`)로 저장, 읽을 때 `datetime.fromisoformat()`. 로컬 시간 naive 사용.

## 4. 규칙 엔진 — `core/rules.py`

```python
def evaluate(
    product: Product,
    current: PricePoint,
    store: Store,
    rules: list[AlertRule],
    cooldown_hours: int = 12,
    renotify_on_lower: bool = True,
) -> list[AlertEvent]:
    ...
```

**호출 계약(중요)**: `evaluate()`는 반드시 현재 가격(`current`)을 `store.add_price()`로 저장하기
**전에** 호출되어야 한다(`core/scheduler.py`가 이 순서를 지킨다: `evaluate()` → `store.add_price(pp)`).
따라서 이 시점에 DB에 들어있는 가장 최근 행(`store.latest_price(pid)`)이 곧 **'직전 확인'** 값이다.
`store.previous_price(pid)`(SQL `OFFSET 1`)는 이미 저장된 현재가를 기준으로 "그 이전"을 가리키므로,
저장 전 호출인 `evaluate()` 안에서 쓰면 한 칸 더 과거를 보게 되어 틀린다 — 실제로 2026-09-04에
`_check_target_price` / `_check_restock` / `evaluate()`(본체의 `prev_price` 조회) 세 곳이 이
off-by-one으로 `previous_price()`를 쓰고 있었고, 특히 **RESTOCK 감지가 한 확인 사이클씩 밀리는**
버그로 나타나 `store.latest_price(pid)`로 교체했다. 새로 규칙을 추가할 때도 "직전 확인"이
필요하면 반드시 `latest_price()`를 쓸 것.

판정 로직:

| kind | 조건 |
|---|---|
| TARGET_PRICE | `current.price is not None and current.price <= threshold` |
| DISCOUNT_RATE | `current.discount_rate is not None and current.discount_rate >= threshold` |
| DISCOUNT_SPIKE | `avg = store.avg_discount_rate(pid, 7)`; `avg is not None and current.discount_rate - avg >= threshold` |
| LOWEST_EVER | `prev_min = store.min_price(pid)` (현재 기록 **제외**); `prev_min is not None and current.price < prev_min` |
| RESTOCK | `store.latest_price(pid).availability == OUT_OF_STOCK and current.availability == IN_STOCK` (직전 확인 = 저장 전 최신 행, 위 호출 계약 참고) |
| UNIT_PRICE | `product.protein_grams` 있고 `current.price / protein_grams <= threshold` |

**쿨다운**: `store.last_alert(pid, kind)`가 `cooldown_hours` 이내면 억제. 단 `renotify_on_lower=True`이고 `current.price < last_alert.price`면 억제 해제.
**품절 시**: `availability == OUT_OF_STOCK`이면 RESTOCK 외 모든 규칙 스킵.
**price is None**(파싱 실패) 시: 어떤 알림도 발생시키지 않음.

`message` 포맷 예:
- `"52,900원 → 41,900원 (▼21%) · 목표가 45,000원 도달"`
- `"39,900원 · 역대 최저가 갱신! (이전 최저 42,000원)"`

`detail`(토스트 셋째 줄)은 `_build_detail()`이 만든다 — 정가·할인율·쿠폰 적용가·직전 확인가(단,
`message`에 이미 `→`로 직전가↔현재가가 들어간 경우엔 중복이라 생략)·단백질 1g당 단가·품절 여부를
있는 정보만 골라 `" · "`로 이어붙인다. 예: `"정가 101,960원에서 7% 할인 · 쿠폰 적용가 · 단백질 1g당 87.0원"`.

## 5. 알림 — `core/notifier.py`

```python
class Notifier:
    def __init__(self, app_id: Optional[str] = None, icon_path: Optional[Path] = None, sound: bool = False) -> None
        # app_id=None이면 config의 notify_app_id → 없으면 DEFAULT_APP_ID("프로틴 할인 알림")
    def notify(self, ev: AlertEvent) -> None     # 토스트, 본문 클릭/버튼 → ev.url 열기
    def notify_error(self, title: str, message: str) -> None
    def notify_info(self, title: str, message: str) -> None

    # 내부 구조
    def _build_toast_xml(self, title, message, detail="", launch_url="", action_label=None, action_url=None) -> str
        # ToastGeneric 템플릿 XML 문자열을 만든다. 본문 최대 3줄(제목/message/detail) +
        # appLogoOverride 이미지(아이콘 있을 때만) + <actions> 버튼(action_label/action_url 있을 때만).
    def _show_toast(self, title, message, detail="", launch_url="", action_label=None, action_url=None) -> None
        # _build_toast_xml()로 만든 XML을 _PS_TEMPLATE에 꽂아 UTF-8 BOM .ps1 파일로 저장하고
        # `powershell -File <path>` 로 실행한 뒤 stdout/stderr를 확인한다(아래 5-1 참고).
```

`winotify`는 쓰지 않는다 — 아래 5-1의 "과거 이력" 참고. `notify()`는 `_show_toast()`에
`launch_url=ev.url`, `action_label="상품 열기"`, `action_url=ev.url`을 넘겨 본문 클릭과
버튼 클릭 둘 다 `ev.url`을 열도록 한다. `AlertEvent.detail`(1절)이 토스트 셋째 줄로 들어간다.

### 5-1. 구현 방식 및 AUMID 등록

**과거 이력 — winotify를 버린 이유 (2026-09-03/04 실측)**: 원래는 `winotify` 패키지의
`Notification.show()`를 썼다. 그런데 winotify 1.1.0에는 구조적 결함이 두 가지 있었다.

1. `winotify/__init__.py`의 `show()`는 토스트 스크립트를 `powershell -Command <스크립트 전체>`
   형태로 **명령줄 인자**로 넘긴다. 스크립트 안에는 PowerShell here-string(`@"` … `"@`)이 들어
   있는데, here-string은 줄바꿈 위치가 정확해야 하는 문법이라 명령줄로 넘기면 깨진다. 그 결과
   `$Template`이 망가져 XML이 비고, Windows가 내용 없는 **"새 알림"** 으로만 표시했다.
2. `_run_ps()`가 PowerShell 스크립트를 `subprocess.Popen(stdout=DEVNULL, stderr=DEVNULL)`로
   **던져놓고 결과를 기다리지 않는다.** 따라서 PowerShell이 `CreateToastNotifier(app_id)`에서
   예외로 죽어도(가장 흔한 원인: app_id가 AUMID로 미등록) 파이썬은 항상 "성공"으로 본다.

두 결함이 겹쳐서 **로그에 "토스트 알림 표시"가 찍혀도 화면에는 아무것도 안 뜨거나, 뜨더라도
내용 없는 "새 알림"만 뜨는** 상태가 됐고 파이썬 쪽에서는 이를 감지할 방법이 없었다.

**현재 구현**: winotify를 완전히 걷어내고 `core/notifier.py`가 직접 PowerShell을 호출한다.

- 토스트 XML(`ToastGeneric` 템플릿, 본문 3줄 + 아이콘 + "상품 열기" 버튼)을
  `_build_toast_xml()`로 직접 만든다.
- 그 XML을 심은 PowerShell 스크립트를 **UTF-8 BOM `.ps1` 파일로 저장**해 `-File`로 실행한다
  (BOM이 없으면 PowerShell이 한글을 ANSI/cp949로 오해해 깨진다. `-Command`로 통째로 넘기면
  위 1번과 같은 here-string 문제가 재발하므로 파일 실행 방식을 쓴다).
- `subprocess.run(capture_output=True)`로 **stderr를 반드시 확인**하고, 성공 판정은 표준출력의
  `TOAST_OK` 문자열 존재 여부로 한다. 실패하면 `logger.error("토스트 알림 실패(rc=%s): ...")`로
  남긴다 — 더 이상 조용한 실패가 없다.

AUMID 자동 등록은 winotify를 쓰던 시절과 동일하게 유지된다: `Notifier.__init__`이
`HKCU\SOFTWARE\Classes\AppUserModelId\<app_id>` 레지스트리 키에 `DisplayName`/`IconUri`를 써서
**AUMID를 자동 등록**한다(`register_aumid()`, `winreg`, 관리자 권한 불필요, 멱등). 이미 등록된
AUMID는 덮어쓰지 않는다(시스템 소유 AUMID 보호).

**전달 여부 검증법**(사람 눈 없이 확인 가능): `HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\<app_id>` 의
`PeriodicNotificationCount` / `LastNotificationAddedTime` 이 증가하면 OS 알림 파이프라인까지
도달한 것이다. 그럼에도 안 보이면 집중 지원(방해 금지) 모드를 의심한다.

모든 알림 실패는 예외를 밖으로 던지지 말고 로그만 남긴다.

## 6. 설정 — `config.json` 스키마

```json
{
  "poll_interval_minutes": 30,
  "jitter_minutes": 5,
  "cooldown_hours": 12,
  "renotify_on_lower": true,
  "sound": false,
  "request_delay_seconds": [5, 15],
  "headless": true,
  "notify_app_id": null,
  "theme": "light",
  "font_family": "Noto Sans KR",
  "font_size": 10,
  "products": [
    {
      "product_id": "1234567890",
      "url": "https://www.coupang.com/vp/products/1234567890?itemId=1&vendorItemId=2",
      "name": "마이프로틴 임팩트 웨이 1kg",
      "protein_grams": 800.0,
      "enabled": true,
      "rules": [
        {"kind": "target_price", "threshold": 45000, "enabled": true},
        {"kind": "lowest_ever",  "threshold": 0,     "enabled": true}
      ]
    }
  ]
}
```

`core/config.py`:
```python
@dataclass
class AppConfig:
    poll_interval_minutes: int = 30
    jitter_minutes: int = 5
    cooldown_hours: int = 12
    renotify_on_lower: bool = True
    sound: bool = False
    request_delay_seconds: tuple[int, int] = (5, 15)
    headless: bool = True                                # 창을 화면 밖에서 띄움(진짜 헤드리스 아님)
    notify_app_id: Optional[str] = None                  # 토스트가 안 뜰 때 쓸 대체 AUMID
    theme: str = "light"                                  # 설정 창 UI 테마. "light" 또는 "dark" (sv-ttk)
    font_family: str = "Noto Sans KR"                      # 설정 창 UI 글꼴 이름(한글 글꼴만)
    font_size: int = 10                                   # 설정 창 UI 글꼴 크기
    products: list[dict] = field(default_factory=list)   # 원본 dict 유지

def load_config(path: Path = CONFIG_FILE) -> AppConfig     # 없으면 기본값으로 생성
def save_config(cfg: AppConfig, path: Path = CONFIG_FILE) -> None
def rules_of(product_entry: dict) -> list[AlertRule]
def product_of(product_entry: dict) -> Product
```

## 7. 수집기 — `core/scraper.py` (구현 완료, SeleniumBase UC 모드로 전면 재작성됨)

기존에는 `undetected-chromedriver`로 수집했으나 더 이상 동작하지 않아(Chrome 151 대응 불가)
**SeleniumBase UC 모드로 전면 교체**했다.

```python
class ScraperBusyError(RuntimeError): ...   # 다른 작업이 브라우저를 점유 중

class CoupangScraper:
    def __init__(self, headless: bool = False, profile_dir: Optional[Path] = None) -> None
        # profile_dir 은 하위 호환을 위해 인자만 받고 쓰지 않는다(아래 7-1 참고).
    def start(self) -> None          # 브라우저 락 획득 → SeleniumBase Driver(uc=True) 기동 → 세션 워밍업
    def stop(self) -> None           # 브라우저 종료 + 락 반납 (몇 번 불러도 안전)
    def fetch(self, url: str) -> PricePoint    # 실패해도 예외 없이 price=None 반환
    def probe(self, url: str) -> Product       # 신규 등록용 메타 조회
    def probe_dict(self, url: str) -> dict     # 설정 GUI용 어댑터
    def __enter__ / __exit__
```

### 7-1. Akamai 우회 — 실측 결과 (2026-09-03)

| 방식 | 결과 |
|---|---|
| `requests` + 완전한 브라우저 헤더 | ❌ 403 (모바일 도메인 루트까지 전부 차단) |
| 모바일 사이트(m.coupang.com) / 내부 JSON API 추정 경로 | ❌ 403 |
| Selenium 헤드리스 | ❌ 403 |
| Selenium 일반 창 (Chrome / Edge) | ❌ `_abck` 쿠키가 봇 판정 |
| undetected-chromedriver 3.5.5 | ❌ Chrome 151에서 더는 못 피함 |
| nodriver 0.50.3 | ❌ 챌린지 단계까지만 도달 |
| 자동화 플래그 없는 순정 브라우저에 나중에 CDP 접속 | △ 홈·검색은 통과, 상품에서 차단 |
| **SeleniumBase UC 모드** | ✅ **통과** |

**왜 SeleniumBase UC 모드만 통과하는가**: `uc_open_with_reconnect(url, reconnect_time)`은 페이지를
여는 동안 자동화 연결(CDP)을 아예 끊었다가, 로딩이 끝난 뒤 다시 붙는다. Akamai의 행동분석 센서가
도는 바로 그 순간에 자동화 흔적이 없어서 봇으로 판정되지 않는다.

일반 Selenium은 CDP가 계속 붙어 있어 센서에 잡히고, 그 결과 `_abck` 쿠키가 봇 판정
(`~-1~`) 상태로 굳는다. 이 상태에서는 홈페이지는 열려도 `/np/search`, `/vp/products` 같은
보호 경로만 403이 난다(`_abck` 값을 직접 확인해 이 진단을 확정했다).

추가로 지켜야 할 제약:

* **헤드리스는 무조건 차단된다.** 그래서 `headless=True`여도 진짜 헤드리스를 쓰지 않고
  `Driver(window_position=OFFSCREEN_POSITION)` 으로 **기동 시점부터** 창을 화면 밖
  (`-32000,-32000`)에 띄운다. 띄운 뒤에 `set_window_position`으로 옮기면 창이 잠깐
  화면에 번쩍이며 사용자의 작업 창 위로 튀어나오기 때문이다(기동 인자가 무시될 경우를
  대비한 사후 보정은 이중 안전장치로 남겨둔다). 화면 밖 배치로도 쿠팡 통과를 실측 확인했다.
* **상품 URL 직접 진입은 차단된다.** UC 모드에서도 마찬가지다. 반드시
  `홈(HOME_URL) → 검색(SEARCH_URL) → 상품` 순서로 자연스럽게 이동해야 통과한다.
* **영구 프로필을 쓰지 않는다.** 매 `start()`마다 SeleniumBase가 깨끗한 임시 프로필로
  브라우저를 새로 띄운다. `__init__`의 `profile_dir` 인자는 하위 호환을 위해 받기만 하고
  실제로는 쓰이지 않는다. 한 번 봇으로 찍힌 프로필은 `_abck`가 오염된 채 굳어버리므로,
  프로필을 재사용하지 않는 쪽이 오히려 안정적이다.
* 한 사이클에 브라우저를 한 번만 띄우고(`start()`) 등록된 상품들을 순회한 뒤 닫는다(`stop()`).

### 7-2. 내부 구조 — `_open` / `_wait_ready` / `_page_state`

```python
def _open(self, url: str, reconnect: int = RECONNECT_PAGE) -> None
    # driver.uc_open_with_reconnect(url, reconnect)로 연다.
    # UC 전용 메서드가 없으면(드라이버 초기화 이상 등) driver.get(url)로 대체한다.

def _page_state(self) -> tuple[str, int]
    # page_source를 검사해 다음 중 하나를 (상태, 길이)로 반환한다:
    #   "access_denied" : "Access Denied" 포함 + 길이 < 3000  (기다려도 안 풀림)
    #   "challenge"     : selectors["challenge_marker"](기본 "sec-if-cpt-container") 포함
    #   "coupang_403"   : selectors["error_marker"](기본 "error403") 포함
    #   "too_short"     : 길이 < selectors["min_page_length"](기본 30000)
    #   "ok"            : 위에 해당 없음 — 정상 페이지로 간주
    #   "exception"     : page_source 접근 자체가 실패

def _wait_ready(self, timeout: int = 45) -> bool
    # _page_state()를 1.5초 간격으로 폴링한다.
    # "ok"면 True. "access_denied"면 기다려도 안 풀리므로 즉시 포기.
    # 그 외 상태는 timeout까지 재시도하다 실패하면 False (self._last_state에 마지막 상태 기록)
```

`_warm_up()`은 `HOME_URL` → (2~4초 랜덤 대기) → `SEARCH_URL` → (2~4초 랜덤 대기) 순으로 `_open` +
`_wait_ready`를 호출해 신뢰 세션을 만든다. `fetch()`/`probe()`는 상품 페이지를 연 뒤 `_wait_ready`가
실패하고 그 원인이 차단성 상태(`access_denied`/`coupang_403`/`too_short`)이면, `_warm_up()`을 다시
태우고 2~5초 대기 후 **딱 1회** 재시도한다. 그래도 실패하면 예외 없이 `price=None`
(`extra_note="page_not_ready:<상태>"`)인 `PricePoint`를 돌려준다.

### 7-3. 브라우저 동시 사용 금지 — `ScraperBusyError`

브라우저는 무겁고 두 개가 동시에 뜨면 서로 방해한다(예: 예약 점검 중에 설정 창에서
'상품 추가'를 누른 경우). 모듈 전역 `_BROWSER_LOCK`(`threading.Lock`)으로 직렬화한다.
`start()`는 락 획득에 실패하면(기본 `_LOCK_TIMEOUT_SEC = 300`초 대기) `ScraperBusyError`를 던진다.
기동 도중 예외가 나도 락은 반드시 반납된다(`_release_lock()`). `stop()`은 몇 번을 불러도 안전하다.

### 7-4. 가격 파싱 — 클래스명에 의존하지 않는다

쿠팡은 Tailwind(`twc-*`) 기반이라 클래스명이 자주 바뀐다. 그래서 `core/parser.py`는
브라우저에서 뽑은 **텍스트 + 계산된 스타일**로 판별한다.

* 할인율 = `^\d+%$`
* 정가 = **취소선** 걸린 금액 중 최대값 (쿠폰 적용 전 금액도 취소선이라 2개가 잡힘)
* 최종가 = 취소선 없고 괄호 밖인 금액 중 **폰트가 가장 큰 것**
* 단위단가 = `(10g당 153원)` 같은 괄호 패턴 → 1g당 원으로 환산
* 쿠폰 = "할인" 문구 + 취소선 금액 2개 이상

### 7-5. 수집용 브라우저 창을 작업표시줄에서 숨기기 — `core/winhide.py` (`TaskbarHider`)

`headless=True`(기본값)로 화면 밖(`OFFSCREEN_POSITION`, `-32000,-32000`)에 창을 띄워도 **작업표시줄
버튼은 남는다.** 게다가 SeleniumBase UC 모드는 페이지를 열 때마다 자동화 연결(CDP)을 끊었다가 다시
붙이는데(7-1절 참고), 그때마다 창이 다시 표시되면서 작업표시줄 버튼이 깜빡인다. 사용자가 "번쩍번쩍
거슬린다"고 보고한 문제다.

**실측 결과 (2026-09-04)**:

| 방법 | 지속성 | 수집 정상? |
|---|---|---|
| `WS_EX_TOOLWINDOW` 확장 스타일 부여 | ✅ 한 번 적용하면 계속 유지 | ✅ 정상 |
| `ShowWindow(SW_HIDE)` 완전 숨김 | ❌ 페이지 이동마다 다시 보임 | ✅ 정상 |

`WS_EX_TOOLWINDOW`를 채택했다. 이 스타일이 붙은 창은 작업표시줄에 나타나지 않고, UC 모드가 창을
다시 표시해도 스타일은 유지된다(3회 이동 연속 검증). 창은 브라우저 기동 후 약 4초 뒤에 생기므로,
`TaskbarHider`는 **브라우저를 띄우기 전부터** 50ms 간격으로 감시 스레드를 돌려 창이 생기는 즉시
스타일을 입힌다. 실측상 작업표시줄에 버튼이 보이는 시간은 최대 50ms 남짓이라 사람 눈에는 잡히지
않는다. 감시 시작 시점에 이미 열려 있던 크롬 창(사용자가 원래 쓰던 창)은 `_known` 집합에 기억해
두고 건드리지 않는다. 실패해도 절대 예외를 밖으로 던지지 않는다 — 창 숨기기가 안 되는 것보다
수집이 멈추는 게 훨씬 나쁘기 때문이다.

```python
class TaskbarHider:
    def __init__(self, window_class: str = CHROME_WINDOW_CLASS) -> None: ...
    def start(self) -> None    # 감시 시작. 브라우저를 띄우기 직전에 호출할 것
    def stop(self) -> None     # 감시 중지. 몇 번을 불러도 안전
    @property
    def hidden_count(self) -> int
```

`CoupangScraper.start()`는 `hide_window`(=`headless`)가 참일 때 `Driver(**kwargs)`를 만들기 **전에**
`TaskbarHider()`를 만들어 `start()`하고, `stop()`(또는 기동 실패 시 예외 경로)에서 `_stop_hider()`로
정리한다. Windows API를 쓸 수 없는 환경(비-Windows)에서는 `_win32()`가 `None`을 반환해 조용히
아무 일도 하지 않는다.

**검증(2026-09-04)**: 새 수집창 작업표시줄 노출 0회(페이지 2회 이동 후에도 유지), 가격 수집
정상(87,820원/45,900원), 사용자 기존 크롬 창 10개 보존, 종료 후 잔여 창 0개.

### 7-6. 품절 감지 — `selectors.json`의 `sold_out`

쿠팡은 상품이 품절이면 `.price-container` 자체를 렌더링하지 않는다. 실제 품절 표시 요소는
`<span class="prod-not-find-known__buy__info__txt">품절</span>` 같은 형태다. 기존
`sold_out` 셀렉터 목록(`.oos-label`, `[class*='oos-']`, `[class*='sold-out']`,
`[class*='soldout']`)이 이 요소를 못 잡아서, 품절 상품이 "가격 파싱 실패"로 계속 잡히는 버그가
있었다(실제로 사용자가 등록한 EVLUTIONNUTRITION 상품에서 재현됨). `selectors.json`의 `sold_out`
목록에 `[class*='prod-not-find']`, `[class*='buybox']`를 추가해 코드 수정 없이 해결했다
(정상 판매 상품 오탐 없음 확인). `_detect_sold_out()`(`core/scraper.py`)은 이 셀렉터들로 찾은
요소의 텍스트에 `sold_out_texts`(품절/일시품절/판매중지 등) 중 하나가 들어있으면 품절로 판정한다.

품절이면 `price_container`가 없어 `price=None`인 `PricePoint`가 만들어지고, 로그에는
`no_price_container+soldout`(정상적으로 감지된 품절)과 `no_price_container`(원인 불명, 셀렉터
확인 필요)를 구분해 남긴다(`core/scraper.py`의 `_open`/`fetch` 경로, `extra_note` 참고).

**파생 버그 수정 — `core/scheduler.py`의 `_track_fetch_result()`**: 이 메서드는 연속 파싱 실패를
세어 3회 연속이면 "가격 파싱 실패" 오류 알림을 보낸다. 그런데 품절(`price=None`)도 파싱 실패와
겉모습이 같아서, 재입고 알림을 기다리며 등록해 둔 품절 상품마다 오류 알림이 잘못 날아가고
있었다. 이제 `pp.availability == Availability.OUT_OF_STOCK`이면 실패로 세지 않고 곧바로
`return`하며(연속 실패 카운터도 `pop`으로 초기화), 진짜 파싱 실패(`price is None`이고 품절도
아닌 경우)만 카운터를 올린다. 검증: 품절 4회 연속 점검 → 오류 알림 0회 / 진짜 파싱 실패 4회 →
오류 알림 1회(3회째 도달 시 1회).

## 8. 로깅 — `core/logging_setup.py`

```python
def setup_logging(level: int = logging.INFO) -> logging.Logger
```
`data/logs/app.log` 로 `RotatingFileHandler`(5MB×3) + 콘솔. 포맷 `%(asctime)s [%(levelname)s] %(name)s: %(message)s`.

## 9. 코딩 규약

- 한국어 주석/메시지 OK. 파일은 UTF-8.
- 외부 I/O(브라우저, 토스트, 파일)는 반드시 try/except로 감싸고 로그 남길 것. 앱이 죽으면 안 됨.
- 각 모듈 하단에 `if __name__ == "__main__":` 자체 테스트 블록을 둘 것.
- 타입힌트 필수. `from __future__ import annotations` 사용.


## 10. 부가 도구 (구현 완료)

| 파일 | 역할 |
|---|---|
| `main.py` | 진입점. 단일 인스턴스 뮤텍스, 스케줄러 + 트레이 배선, 설정 저장 시 즉시 반영 |
| `ui/tray.py` | 트레이 아이콘. 지금 확인 / 일시정지 / 설정 / 로그 폴더 / 종료 |
| `tools/연결테스트.py` | 프로그램을 켜지 않고 수집 기능만 진단 (probe + fetch, 실패 원인별 대처법 안내) |
| `tools/알림테스트.py` | 토스트가 안 뜰 때 방식별 4종을 띄워 원인 위치를 좁히는 진단 도구 |
| `tools/바로가기_만들기.py` | 바탕화면 / 시작 메뉴 / 자동 실행 바로가기 생성·제거 (`--상태`, `--제거`, `--자동시작`). 폴더 위치는 특수 폴더 API로 얻고(바탕화면 리디렉션 대응), 아이콘은 `ui/icon.ico` 지정 |
| `core/winhide.py` | 수집용 브라우저 창을 작업표시줄에서 숨김(`TaskbarHider`, `WS_EX_TOOLWINDOW`, Windows 전용, 7-5절 참고) |
| `실행.bat` / `디버그실행.bat` | 더블클릭 실행 / 로그 보며 실행 |

## 11. 검증 완료 항목

* `core/parser.py` 자체 테스트 9케이스 통과
* `core/rules.py` 규칙 12케이스 통과
* `core/store.py` 스키마·스레드 안전성 검증
* `core/scheduler.py` 6시나리오 + 백오프 검증
* **실제 쿠팡 E2E (2026-09-03, SeleniumBase UC 모드로 재검증)**: 뉴욕웨이 골드 WPI
  45,900원(정가 79,900원, 42%, 15.3원/g), 뉴욕웨이 라이트 36,700원(정가 99,000원, 63%, 18.4원/g)
  수집 성공. 상품명 자동 조회(`probe()`)도 정상 동작 확인.
* 브라우저 락(`_BROWSER_LOCK`): 충돌 거절(`ScraperBusyError`) / 반납 후 재획득 / 기동 실패 시 누수 없음
* **작업표시줄 숨김(2026-09-04, `TaskbarHider`, 7-5절)**: 새 수집창 작업표시줄 노출 0회(페이지
  2회 이동 후에도 유지), 가격 수집 정상(87,820원/45,900원), 사용자 기존 크롬 창 10개 보존,
  종료 후 잔여 창 0개
* **품절 감지 수정(2026-09-04, `selectors.json`의 `sold_out`, 7-6절)**: 정상 판매 상품 오탐
  없음 확인. `_track_fetch_result()` 품절 예외 처리 — 품절 4회 연속 점검 시 오류 알림 0회,
  진짜 파싱 실패 4회 연속 시 오류 알림 1회(3회째 도달 시)
