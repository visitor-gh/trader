"""비밀정보(API 키, 계좌번호) 보호.

원칙
  1) 비밀값은 코드·설정파일·Git에 절대 넣지 않는다 → 환경변수 또는 OS 키체인(keyring)으로만 주입.
  2) 로그·리포트·예외 메시지에는 마스킹된 값만 남긴다(SecretMaskingFilter).
  3) 디스크에 저장해야 하는 민감 데이터는 대칭키 암호화(Fernet: AES-128-CBC + HMAC-SHA256).
  4) 파일 권한은 소유자 전용(0600)인지 점검한다.
"""
from __future__ import annotations

import logging
import os
import re
import stat


def load_secret(name: str, required: bool = True) -> str:
    """환경변수 → keyring 순으로 조회. 값 자체는 절대 로그에 남기지 않는다."""
    v = os.environ.get(name, "")
    if not v:
        try:
            import keyring  # 선택 의존성

            v = keyring.get_password("ktrader", name) or ""
        except Exception:
            v = ""
    if required and not v:
        raise RuntimeError(f"비밀값 {name} 이(가) 설정되지 않았습니다(환경변수 또는 keyring).")
    return v


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - keep) + value[-2:]


_PATTERNS = [
    re.compile(r"(?i)(appkey|appsecret|app_key|app_secret|authorization|access_token|crtfc_key|password)(\"?\s*[:=]\s*\"?)(Bearer\s+)?([^\s\"',}]+)"),
    re.compile(r"\b(\d{8})-?(\d{2})\b"),  # 계좌번호 형식
]


def redact(text: str) -> str:
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}***", text)
    return _PATTERNS[1].sub(lambda m: f"{m.group(1)[:2]}******-**", text)


class SecretMaskingFilter(logging.Filter):
    """모든 로그 레코드에서 키·토큰·계좌번호 패턴을 자동 마스킹."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.getMessage()))
        record.args = ()
        return True


def install_log_masking(logger: logging.Logger | None = None) -> None:
    lg = logger or logging.getLogger()
    for h in lg.handlers or [logging.StreamHandler()]:
        h.addFilter(SecretMaskingFilter())
        if h not in lg.handlers:
            lg.addHandler(h)


class DataCipher:
    """민감 데이터 암호화. 키는 TRADER_DATA_KEY 환경변수(Fernet.generate_key()로 생성)."""

    def __init__(self, key: str | bytes | None = None):
        from cryptography.fernet import Fernet

        key = key or load_secret("TRADER_DATA_KEY")
        self._f = Fernet(key if isinstance(key, bytes) else key.encode())

    def encrypt(self, plain: str) -> str:
        return self._f.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()


def check_file_permissions(path: str) -> list[str]:
    """그룹/기타 사용자에게 읽기 권한이 있으면 경고 목록 반환(POSIX)."""
    if not os.path.exists(path) or os.name != "posix":
        return []
    mode = os.stat(path).st_mode
    issues = []
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        issues.append(f"{path}: 다른 사용자가 읽을 수 있음 → chmod 600 권장")
    return issues
