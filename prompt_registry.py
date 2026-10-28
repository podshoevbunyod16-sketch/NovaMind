"""prompt_registry.py — версионирование промптов и управление их жизненным циклом.

Проблема
--------
Промпты агентов лежали текстовыми файлами в ``agenticseek/prompts/base`` и
загружались по имени. Из-за этого:

* правка промпта «на месте» меняла поведение агента без следа: нельзя было
  сказать, какой текст реально был в конкретном ходе и что изменилось после
  «стало хуже»;
* не было способа откатиться к предыдущей версии — только вручную, из git;
* одинаковые промпты в разных местах расходились, а тесты проверяли лишь
  наличие файла;
* нельзя было сравнить две версии и понять, что именно поменяли.

Что здесь есть
--------------
* **Версия = содержимое**: sha256 файла (первые 12 символов). Версия не
  зависит от даты и имени ветки, её видно в трассе каждого хода.
* **Архив**: при первом обращении и при каждом изменении текущий текст
  сохраняется в ``agenticseek/prompts/versions/<имя>/<версия>.txt``, а запись
  о версии попадает в ``manifest.json`` (кто, когда, размер).
* **Закрепление**: ``PROMPT_VERSION_<ИМЯ>=<версия>`` в окружении или
  ``pin()`` в манифесте заставляет агента использовать конкретную версию —
  это и есть безопасный откат.
* **Сравнение**: :func:`diff` возвращает unified diff двух версий.
* **Аудит**: :func:`snapshot_all` обновляет манифест, :func:`list_prompts`
  отдаёт состояние для API, :func:`record_use` пишет версию в трассу.

Манифест обновляется атомарно (запись во временный файл + ``os.replace``),
поэтому параллельные запуски не оставят полуфайл.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import time
from typing import Iterable

log = logging.getLogger("novamind.prompts")

PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "agenticseek", "prompts")
BASE_DIR = os.path.join(PROMPT_DIR, "base")
VERSIONS_DIR = os.path.join(PROMPT_DIR, "versions")
MANIFEST_PATH = os.path.join(PROMPT_DIR, "manifest.json")

_NAME_RE = re.compile(r"[A-Za-z0-9_\-]+")
_ENV_PREFIX = "PROMPT_VERSION_"


def _normalize(name: str) -> str:
    name = str(name or "").strip()
    if name.endswith(".txt"):
        name = name[:-4]
    if not name or not _NAME_RE.fullmatch(name):
        raise ValueError(f"Недопустимое имя промпта: {name!r}")
    return name


def _env_key(name: str) -> str:
    return _ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def version_of_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


def version_of_file(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:12]


def _read_manifest() -> dict:
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"version": 1, "updated_at": 0, "prompts": {}}
    if not isinstance(data, dict):
        return {"version": 1, "updated_at": 0, "prompts": {}}
    data.setdefault("prompts", {})
    return data


def _write_manifest(manifest: dict) -> None:
    manifest["updated_at"] = round(time.time(), 3)
    directory = os.path.dirname(MANIFEST_PATH)
    os.makedirs(directory, exist_ok=True)
    temporary = MANIFEST_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, MANIFEST_PATH)


def base_path(name: str) -> str:
    return os.path.join(BASE_DIR, f"{_normalize(name)}.txt")


def archive_path(name: str, version: str) -> str:
    return os.path.join(VERSIONS_DIR, _normalize(name), f"{version}.txt")


def available() -> list[str]:
    try:
        return sorted(item[:-4] for item in os.listdir(BASE_DIR)
                      if item.endswith(".txt"))
    except OSError:
        return []


def snapshot(name: str) -> dict:
    """Фиксирует текущее содержимое промпта как версию. Идемпотентно."""
    name = _normalize(name)
    source = base_path(name)
    if not os.path.isfile(source):
        raise FileNotFoundError(f"Промпт не найден: {name}")
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    version = version_of_text(text)
    manifest = _read_manifest()
    entry = manifest["prompts"].setdefault(name, {"versions": []})
    known = {item.get("version") for item in entry.get("versions", [])}
    changed = entry.get("active") != version
    if version not in known:
        entry.setdefault("versions", []).append({
            "version": version, "created_at": round(time.time(), 3),
            "chars": len(text), "lines": text.count("\n") + 1,
            "source": os.path.relpath(source, os.path.dirname(PROMPT_DIR)),
        })
    entry["active"] = version
    if changed or version not in known:
        target = archive_path(name, version)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not os.path.isfile(target):
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(text)
        _write_manifest(manifest)
        log.info("prompts: %s → версия %s", name, version)
    return {"name": name, "version": version, "path": source, "changed": changed}


def snapshot_all(names: Iterable[str] | None = None) -> list[dict]:
    result = []
    for name in (list(names) if names is not None else available()):
        try:
            result.append(snapshot(name))
        except (FileNotFoundError, ValueError):
            log.warning("prompts: пропускаю %s", name, exc_info=True)
    return result


def pinned_version(name: str) -> str:
    """Версия, закреплённая окружением или манифестом ('' — не закреплена)."""
    name = _normalize(name)
    env_value = (os.getenv(_env_key(name)) or "").strip()
    if env_value:
        return env_value
    entry = _read_manifest()["prompts"].get(name) or {}
    return str(entry.get("pinned") or "").strip()


def resolve(name: str) -> str:
    """Путь к актуальной (или закреплённой) версии промпта.

    Если закреплённая версия отсутствует в архиве — честно пишем в журнал и
    используем текущую: молча подменять промпт нельзя, но и ронять ход из-за
    опечатки в переменной окружения тоже.
    """
    name = _normalize(name)
    current = base_path(name)
    pin = pinned_version(name)
    if not pin:
        return current
    candidate = archive_path(name, pin)
    if os.path.isfile(candidate):
        return candidate
    log.warning("prompts: закреплённая версия %s для %s не найдена — беру текущую",
                pin, name)
    return current


def load(name: str) -> str:
    path = resolve(name)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def pin(name: str, version: str) -> dict:
    """Закрепляет версию в манифесте (без переменных окружения)."""
    name, version = _normalize(name), str(version or "").strip()
    if not re.fullmatch(r"[0-9a-f]{8,64}", version):
        raise ValueError("Версия должна быть sha256-префиксом (hex)")
    if not os.path.isfile(archive_path(name, version)):
        raise FileNotFoundError(f"Версия {version} для {name} отсутствует в архиве")
    manifest = _read_manifest()
    entry = manifest["prompts"].setdefault(name, {"versions": []})
    entry["pinned"] = version
    entry["pinned_at"] = round(time.time(), 3)
    _write_manifest(manifest)
    return {"name": name, "pinned": version}


def unpin(name: str) -> dict:
    name = _normalize(name)
    manifest = _read_manifest()
    entry = manifest["prompts"].get(name)
    if entry:
        entry.pop("pinned", None)
        entry.pop("pinned_at", None)
        _write_manifest(manifest)
    return {"name": name, "pinned": ""}


def history(name: str) -> list[dict]:
    name = _normalize(name)
    entry = _read_manifest()["prompts"].get(name) or {}
    # Порядок в манифесте — хронологический (append), поэтому новые версии
    # отдаём первыми, не полагаясь на created_at: два снимка в одну секунду
    # иначе получали один timestamp и «активной» оказывалась старая версия.
    versions = [dict(item) for item in reversed(entry.get("versions", []))]
    active = entry.get("active") or (version_of_file(base_path(name))
                                     if os.path.isfile(base_path(name)) else "")
    pinned = pinned_version(name)
    for item in versions:
        item["active"] = item.get("version") == active
        item["pinned"] = item.get("version") == pinned
    return versions


def diff(name: str, old: str, new: str = "") -> str:
    """Unified diff версий промпта (``new`` по умолчанию — текущая)."""
    name = _normalize(name)
    old_path = archive_path(name, old)
    new_path = archive_path(name, new) if new else base_path(name)
    try:
        with open(old_path, "r", encoding="utf-8") as handle:
            old_text = handle.read()
        with open(new_path, "r", encoding="utf-8") as handle:
            new_text = handle.read()
    except OSError as error:
        raise FileNotFoundError(str(error)) from error
    return "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"{name}@{old}", tofile=f"{name}@{new or 'current'}"))


def record_use(name: str) -> dict:
    """Версия, которая реально ушла в модель. Для атрибутов трассы."""
    name = _normalize(name)
    path = resolve(name)
    return {"name": name, "version": version_of_file(path),
            "pinned": bool(pinned_version(name)),
            "path": os.path.relpath(path, os.path.dirname(PROMPT_DIR))}


def record_use_all(names: Iterable[str] | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in (list(names) if names is not None else available()):
        try:
            result[name] = record_use(name)["version"]
        except (OSError, ValueError):
            continue
    return result


def list_prompts() -> list[dict]:
    """Состояние всех промптов для API/интерфейса."""
    result = []
    for name in available():
        entry = _read_manifest()["prompts"].get(name) or {}
        current = version_of_file(base_path(name))
        pinned = pinned_version(name)
        result.append({
            "name": name,
            "version": current,
            "active": entry.get("active") or current,
            "pinned": pinned,
            "pinned_by_env": bool((os.getenv(_env_key(name)) or "").strip()),
            "chars": os.path.getsize(base_path(name)),
            "versions": len(entry.get("versions", [])),
            "updated_at": entry.get("versions", [{}])[-1].get("created_at")
            if entry.get("versions") else None,
        })
    return result


def summary() -> dict:
    return {"directory": PROMPT_DIR, "manifest": MANIFEST_PATH,
            "prompts": len(available()), "pinned": sum(1 for item in available()
                                                       if pinned_version(item)),
            "env_prefix": _ENV_PREFIX}


def ensure() -> list[dict]:
    """Инициализирует манифест и архив (идемпотентно).

    Вызывается при старте приложения и из API: файлы версий должны появиться
    сразу, а не «внезапно» при первом запросе. Ошибки записи не роняют сервер —
    промпт всё равно загрузится из ``base``.
    """
    try:
        return snapshot_all()
    except Exception:                                       # noqa: BLE001
        log.warning("prompts: не удалось обновить манифест", exc_info=True)
        return []
