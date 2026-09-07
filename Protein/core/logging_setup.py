"""로깅 설정 (SPEC.md 8절).

data/logs/app.log 로 RotatingFileHandler(5MB x 3) + 콘솔 핸들러.
포맷: %(asctime)s [%(levelname)s] %(name)s: %(message)s
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from core.paths import LOG_DIR, ensure_dirs

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_BACKUP_COUNT = 3

_configured = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """루트 로거에 파일(회전) + 콘솔 핸들러를 설정하고 반환한다.

    여러 번 호출해도 핸들러가 중복 등록되지 않도록 멱등하게 동작한다.
    """
    global _configured

    ensure_dirs()

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not _configured:
        formatter = logging.Formatter(_LOG_FORMAT)

        log_file = LOG_DIR / "app.log"
        file_handler = RotatingFileHandler(
            log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)

        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

        _configured = True
    else:
        # 이미 설정된 핸들러들의 레벨만 갱신
        for handler in root_logger.handlers:
            handler.setLevel(level)

    return root_logger


if __name__ == "__main__":
    logger = setup_logging(logging.DEBUG)
    logger.debug("디버그 메시지 테스트")
    logger.info("정보 메시지 테스트")
    logger.warning("경고 메시지 테스트")
    logger.error("에러 메시지 테스트")

    assert LOG_DIR.exists(), "LOG_DIR이 생성되지 않았습니다"
    log_file = LOG_DIR / "app.log"
    assert log_file.exists(), "app.log가 생성되지 않았습니다"

    content = log_file.read_text(encoding="utf-8")
    assert "정보 메시지 테스트" in content

    # 멱등성 확인: 다시 호출해도 핸들러 개수가 늘어나지 않아야 함
    handlers_before = len(logging.getLogger().handlers)
    setup_logging(logging.INFO)
    handlers_after = len(logging.getLogger().handlers)
    assert handlers_before == handlers_after, "핸들러가 중복 등록되었습니다"

    print("OK: logging_setup.py self-test passed")
