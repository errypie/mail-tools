"""Зашифрованное хранилище секретов (логин и пароль Exchange).

Ключ выводится из пароля вебчика (scrypt) и живёт только в памяти процесса.
На диске лежат соль и шифротекст AES-256-GCM: ни пароля, ни ключа там нет.
Проверка пароля при входе — это успешная расшифровка, отдельный хэш не хранится.
"""
from __future__ import annotations

import base64
import json
import os
import threading
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

AAD = b"mailbot-vault-v1"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
MIN_PASSWORD = 8


class VaultError(RuntimeError):
    pass


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


class Vault:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._key: Optional[bytes] = None
        self._salt: Optional[bytes] = None
        self._data: dict = {}

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    def create(self, password: str) -> None:
        with self._lock:
            if self.exists:
                raise VaultError("Хранилище уже создано")
            self._check_strength(password)
            self._salt = os.urandom(16)
            self._key = _derive(password, self._salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
            self._data = {}
            self._save()

    def unlock(self, password: str) -> bool:
        with open(self.path, encoding="utf-8") as f:
            blob = json.load(f)
        kdf = blob["kdf"]
        salt = _unb64(kdf["salt"])
        key = _derive(password, salt, kdf["n"], kdf["r"], kdf["p"])
        try:
            plain = AESGCM(key).decrypt(_unb64(blob["nonce"]), _unb64(blob["ciphertext"]), AAD)
        except InvalidTag:
            return False
        with self._lock:
            self._key, self._salt, self._data = key, salt, json.loads(plain.decode("utf-8"))
        return True

    def get(self, name: str, default=None):
        with self._lock:
            self._require_unlocked()
            return self._data.get(name, default)

    def update(self, **values) -> None:
        with self._lock:
            self._require_unlocked()
            self._data.update(values)
            self._save()

    def change_password(self, old: str, new: str) -> bool:
        with self._lock:
            self._check_strength(new)
            if not self.unlock(old):
                return False
            self._salt = os.urandom(16)
            self._key = _derive(new, self._salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
            self._save()
            return True

    def _check_strength(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise VaultError("Пароль должен быть не короче {} символов".format(MIN_PASSWORD))

    def _require_unlocked(self) -> None:
        if self._key is None:
            raise VaultError("Хранилище заблокировано: нужно войти")

    def _save(self) -> None:
        nonce = os.urandom(12)  # новый nonce на каждую запись: повтор с тем же ключом ломает GCM
        plain = json.dumps(self._data, ensure_ascii=False).encode("utf-8")
        blob = {
            "v": 1,
            "kdf": {"name": "scrypt", "salt": _b64(self._salt), "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
            "nonce": _b64(nonce),
            "ciphertext": _b64(AESGCM(self._key).encrypt(nonce, plain, AAD)),
        }
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(blob, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
