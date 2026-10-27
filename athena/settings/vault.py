"""Encrypted storage for provider keys.

`keyring` is not installed in this environment, so the vault is the same design
the old Athena used and the one that works everywhere: a Fernet key file under
the user's data directory, values encrypted at rest in a JSON file next to it.
The key file is restricted to the current user where the OS supports it.

Nothing here is ever returned by the API: callers ask `has()` or use the value
to build a child process environment.
"""

from __future__ import annotations

import json
import os
import stat
from abc import ABC, abstractmethod
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SETTINGS_DIR = Path("data/settings")
KEY_FILE = SETTINGS_DIR / "master.key"
STORE_FILE = SETTINGS_DIR / "secrets.enc.json"


class VaultError(RuntimeError):
    """A secret could not be stored or read."""


class SecretStore(ABC):
    @abstractmethod
    def get(self, name: str) -> str | None: ...

    @abstractmethod
    def set(self, name: str, value: str) -> None: ...

    @abstractmethod
    def delete(self, name: str) -> None: ...

    @abstractmethod
    def has(self, name: str) -> bool: ...


def _restrict(path: Path) -> None:
    """Best effort: owner-only where the platform honours it."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


class MemoryStore(SecretStore):
    """For tests, and for a vault that must not touch disk."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        if not value:
            raise VaultError("refusing to store an empty secret")
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)

    def has(self, name: str) -> bool:
        return bool(self._values.get(name))


class FernetVault(SecretStore):
    """A single key file plus one encrypted JSON file."""

    def __init__(self, directory: Path = SETTINGS_DIR) -> None:
        self.key_file = Path(directory) / KEY_FILE.name
        self.store_file = Path(directory) / STORE_FILE.name

    def _fernet(self) -> Fernet:
        if not self.key_file.exists():
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            self.key_file.write_bytes(Fernet.generate_key())
            _restrict(self.key_file)
        return Fernet(self.key_file.read_bytes())

    def _read(self) -> dict[str, str]:
        if not self.store_file.exists():
            return {}
        try:
            return json.loads(self.store_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise VaultError("secrets file is corrupted") from exc

    def _write(self, values: dict[str, str]) -> None:
        self.store_file.parent.mkdir(parents=True, exist_ok=True)
        self.store_file.write_text(json.dumps(values), encoding="utf-8")
        _restrict(self.store_file)

    def get(self, name: str) -> str | None:
        token = self._read().get(name)
        if not token:
            return None
        try:
            return self._fernet().decrypt(token.encode()).decode()
        except (InvalidToken, ValueError) as exc:
            raise VaultError(f"cannot decrypt secret {name!r}") from exc

    def set(self, name: str, value: str) -> None:
        if not value:
            raise VaultError("refusing to store an empty secret")
        values = self._read()
        values[name] = self._fernet().encrypt(value.encode()).decode()
        self._write(values)

    def delete(self, name: str) -> None:
        values = self._read()
        if values.pop(name, None) is not None:
            self._write(values)

    def has(self, name: str) -> bool:
        return bool(self._read().get(name))


_VAULT: SecretStore | None = None


def default_vault() -> SecretStore:
    global _VAULT
    if _VAULT is None:
        _VAULT = FernetVault()
    return _VAULT


def set_vault(vault: SecretStore | None) -> None:
    """Tests swap in a memory store; production keeps the Fernet one."""
    global _VAULT
    _VAULT = vault
