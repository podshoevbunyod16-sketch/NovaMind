"""Persistent user skills for NovaMind.

Each skill is a directory containing a validated SKILL.md and optional resources.
The storage location can be changed with NOVAMIND_SKILLS_DIR.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SKILLS_DIR = Path(os.getenv("NOVAMIND_SKILLS_DIR", ROOT / "user_skills")).resolve()
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_SKILL_CHARS = 120_000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def _safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip().lower()).strip("-.")
    if not _NAME_RE.fullmatch(value):
        raise ValueError("Имя skill должно содержать 2–64 символа: a-z, 0-9, -, _, .")
    return value


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        raise ValueError("SKILL.md должен начинаться с YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("В SKILL.md отсутствует закрывающий frontmatter")
    values: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("'\"")
    if not values.get("name") or not values.get("description"):
        raise ValueError("Frontmatter должен содержать name и description")
    return values


def _validate_markdown(text: str) -> dict[str, str]:
    if not text.strip() or len(text) > MAX_SKILL_CHARS:
        raise ValueError(f"SKILL.md должен быть непустым и не длиннее {MAX_SKILL_CHARS} символов")
    return _frontmatter(text)


def _write_skill(name: str, markdown: str, source_dir: Path | None = None) -> dict[str, Any]:
    name = _safe_name(name)
    meta = _validate_markdown(markdown)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    target = SKILLS_DIR / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(markdown, encoding="utf-8")
    if source_dir:
        for child in source_dir.iterdir():
            if child.name == "SKILL.md":
                continue
            destination = target / child.name
            if child.is_dir():
                shutil.copytree(child, destination)
            else:
                shutil.copy2(child, destination)
    return {"id": name, "name": meta["name"], "description": meta["description"], "path": str(target)}


#: Файл со списком выключенных навыков. Скилл остаётся на диске, но не
#: попадает в промпт агента — это и есть «выключить».
DISABLED_FILE = ".disabled.json"


def _disabled() -> set[str]:
    path = SKILLS_DIR / DISABLED_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(item) for item in data if isinstance(item, str)} if isinstance(data, list) else set()


def _write_disabled(values: set[str]) -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    path = SKILLS_DIR / DISABLED_FILE
    path.write_text(json.dumps(sorted(values), ensure_ascii=False, indent=2), encoding="utf-8")


def is_enabled(skill_id: str) -> bool:
    return _safe_name(skill_id) not in _disabled()


def set_enabled(skill_id: str, enabled: bool) -> dict[str, Any]:
    """Включает или выключает навык. Возвращает обновлённую запись."""
    name = _safe_name(skill_id)
    if not (SKILLS_DIR / name / "SKILL.md").is_file():
        raise FileNotFoundError("Skill не найден")
    disabled = _disabled()
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    _write_disabled(disabled)
    return next(item for item in list_skills() if item["id"] == name)


def _tools_of(meta: dict[str, str]) -> list[str]:
    """Инструменты, которые навык объявляет в frontmatter (``tools: a, b``)."""
    raw = str(meta.get("tools") or meta.get("requires") or "")
    return [item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()]


def list_skills() -> list[dict[str, Any]]:
    if not SKILLS_DIR.exists():
        return []
    disabled = _disabled()
    items = []
    for directory in sorted(SKILLS_DIR.iterdir()):
        skill_file = directory / "SKILL.md"
        if not directory.is_dir() or not skill_file.is_file():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
            meta = _validate_markdown(text)
            items.append({"id": directory.name, "name": meta["name"],
                          "description": meta["description"], "path": str(directory),
                          "updated": skill_file.stat().st_mtime,
                          "enabled": directory.name not in disabled,
                          "tools": _tools_of(meta)})
        except (OSError, ValueError) as exc:
            items.append({"id": directory.name, "name": directory.name,
                          "description": f"Ошибка skill: {exc}", "path": str(directory),
                          "invalid": True, "enabled": directory.name not in disabled,
                          "tools": []})
    return items


def get_skill(skill_id: str) -> dict[str, Any]:
    name = _safe_name(skill_id)
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError("Skill не найден")
    text = path.read_text(encoding="utf-8")
    meta = _validate_markdown(text)
    return {"id": name, "name": meta["name"], "description": meta["description"],
            "content": text, "enabled": is_enabled(name), "tools": _tools_of(meta)}


def save_text(name: str, markdown: str) -> dict[str, Any]:
    return _write_skill(name, markdown)


def import_zip(file_storage) -> dict[str, Any]:
    if not file_storage or not file_storage.filename:
        raise ValueError("Выберите ZIP-архив skill")
    raw = file_storage.read(MAX_ZIP_BYTES + 1)
    if len(raw) > MAX_ZIP_BYTES:
        raise ValueError("ZIP-архив слишком большой: максимум 25 MB")
    with tempfile.TemporaryDirectory(prefix="novamind-skill-") as temp:
        root = Path(temp)
        archive = root / "skill.zip"
        archive.write_bytes(raw)
        with zipfile.ZipFile(archive) as zf:
            for item in zf.infolist():
                name = Path(item.filename)
                if item.filename.startswith("/") or ".." in name.parts:
                    raise ValueError("ZIP содержит небезопасный путь")
            zf.extractall(root / "extracted")
        extracted = root / "extracted"
        candidates = [p for p in extracted.rglob("SKILL.md") if p.is_file()]
        if len(candidates) != 1:
            raise ValueError("ZIP должен содержать ровно один файл SKILL.md")
        skill_file = candidates[0]
        source = skill_file.parent
        meta = _frontmatter(skill_file.read_text(encoding="utf-8"))
        return _write_skill(meta["name"], skill_file.read_text(encoding="utf-8"), source)


def delete_skill(skill_id: str) -> None:
    name = _safe_name(skill_id)
    target = SKILLS_DIR / name
    if target.exists():
        shutil.rmtree(target)
    disabled = _disabled()
    if name in disabled:
        disabled.discard(name)
        _write_disabled(disabled)


def _available_tool_names() -> set[str]:
    """Live unified names used to validate skill requirements."""
    names: set[str] = set()
    try:
        import tool_registry
        names.update(spec.name for spec in tool_registry.list_tools())
        names.update(tool_registry.ACTION_TO_TOOL)
    except Exception:
        pass
    try:
        import mcp_registry
        for item in mcp_registry.connected_tools():
            names.add(str(item.get("name") or ""))
            names.add("mcp_call")
    except Exception:
        pass
    return {name for name in names if name}


def validate_skill(skill_id: str) -> dict[str, Any]:
    """Validate markdown and report unavailable declared tools without executing it."""
    skill = get_skill(skill_id)
    available = _available_tool_names()
    missing = [name for name in skill.get("tools", []) if name not in available]
    return {"ok": not missing, "skill": skill_id, "missing_tools": missing,
            "available_tools": sorted(available)}


def prompt_context(query: str = "", max_chars: int = 18_000, limit: int = 6) -> str:
    """Load only relevant enabled skills into the temporary task context.

    Skills remain persisted globally, but unrelated instructions are not mixed
    into every request.  Declared unavailable tools are shown as a warning so
    the model does not hallucinate their execution.
    """
    words = {w for w in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9_-]{3,}", str(query).lower())}
    ranked = []
    for item in list_skills():
        if item.get("invalid") or not item.get("enabled", True):
            continue
        haystack = f"{item.get('id','')} {item.get('name','')} {item.get('description','')}".lower()
        score = sum(3 if word in haystack else 0 for word in words)
        # Explicitly named skills always win; with an empty query preserve the
        # old behaviour (up to the bounded limit).
        if not words or score or str(item.get("id")) in str(query).lower():
            ranked.append((score, float(item.get("updated") or 0), item))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    blocks = []
    remaining = max_chars
    available = _available_tool_names()
    for _score, _updated, item in ranked[:max(1, min(int(limit), 20))]:
        try:
            skill = get_skill(item["id"])
        except Exception:
            continue
        missing = [name for name in skill.get("tools", []) if name not in available]
        warning = ("\nНедоступные инструменты (не вызывать): " + ", ".join(missing)) if missing else ""
        block = (f"\n## Skill: {skill['name']} ({skill['id']})\n{skill['description']}"
                 f"{warning}\n{skill['content']}\n")
        if len(block) > remaining:
            continue
        blocks.append(block)
        remaining -= len(block)
    return "\n".join(blocks)
