"""Password hashing, one-time codes and request throttling helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata

PASSWORD_ITERATIONS = 210_000
CODE_ITERATIONS = 120_000
SALT_BYTES = 16
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Некорректный формат данных.") from exc


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def encode_salt(salt: bytes) -> str:
    return _b64(salt)


def decode_salt(value: str) -> bytes:
    return _unb64(value)


def _pepper(value: str, secret: str) -> bytes:
    # The deployment secret is mixed in so a stolen database alone is not enough
    # to brute-force passwords offline.
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest()


def _pbkdf2(value: bytes, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", value, salt, iterations)


def hash_password(password: str, *, secret: str) -> str:
    salt = new_salt()
    digest = _pbkdf2(_pepper(password, secret), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str, *, secret: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$")
        iterations = int(iterations_text)
        salt = _unb64(salt_text)
        expected = _unb64(digest_text)
    except (ValueError, TypeError, AttributeError):
        return False
    if algorithm != "pbkdf2_sha256" or iterations < 50_000 or len(expected) != 32:
        return False
    actual = _pbkdf2(_pepper(password, secret), salt, iterations)
    return hmac.compare_digest(actual, expected)


def generate_code(length: int) -> str:
    """Return a numeric one-time code without ambiguous leading zeros for humans."""
    if length < 4:
        raise ValueError("Длина кода должна быть не менее 4.")
    first = secrets.randbelow(9) + 1
    rest = (str(secrets.randbelow(10)) for _ in range(length - 1))
    return f"{first}{''.join(rest)}"


def hash_code(code: str, *, salt: bytes) -> str:
    return _b64(_pbkdf2(code.encode("utf-8"), salt, CODE_ITERATIONS))


def verify_code(code: str, *, salt: bytes, expected: str) -> bool:
    try:
        actual = hash_code(code, salt=salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def ip_hash(ip: str, *, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"ip:{ip}".encode("utf-8"), hashlib.sha256).hexdigest()


def rate_key_hash(bucket: str, key: str, *, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"{bucket}:{key}".encode("utf-8"), hashlib.sha256).hexdigest()


def password_problem(password: str, min_length: int) -> str | None:
    """Return a Russian explanation when the password is too weak, otherwise None."""
    if len(password) < min_length:
        return f"Пароль должен содержать не менее {min_length} символов."
    if len(password) > 256:
        return "Пароль должен содержать не более 256 символов."
    if CONTROL_CHARS_RE.search(password):
        return "Пароль содержит недопустимые символы."
    if not any(character.isalpha() for character in password):
        return "Пароль должен содержать хотя бы одну букву."
    if not any(character.isdigit() for character in password):
        return "Пароль должен содержать хотя бы одну цифру."
    return None


def clean_text(value: str) -> str:
    """Normalize user input: NFC, no control characters, no zero-width marks."""
    normalized = unicodedata.normalize("NFC", value)
    without_control = CONTROL_CHARS_RE.sub("", normalized)
    return without_control.translate({0x200B: None, 0xFEFF: None}).strip()
