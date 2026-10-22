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


def list_skills() -> list[dict[str, Any]]:
    if not SKILLS_DIR.exists():
        return []
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
                          "updated": skill_file.stat().st_mtime})
        except (OSError, ValueError) as exc:
            items.append({"id": directory.name, "name": directory.name,
                          "description": f"Ошибка skill: {exc}", "path": str(directory),
                          "invalid": True})
    return items


def get_skill(skill_id: str) -> dict[str, Any]:
    name = _safe_name(skill_id)
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError("Skill не найден")
    text = path.read_text(encoding="utf-8")
    meta = _validate_markdown(text)
    return {"id": name, "name": meta["name"], "description": meta["description"], "content": text}


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


def prompt_context(max_chars: int = 800) -> str:
    """Короткий каталог «имя: описание». Полный SKILL.md читается отдельно."""
    lines = []
    used = 0
    limit = max(0, int(max_chars))
    for item in list_skills():
        if item.get("invalid"):
            continue
        name = str(item.get("id") or item.get("name") or "").strip()
        description = " ".join(str(item.get("description") or "").split())
        line = f"{name}: {description}" if description else name
        if len(line) > 180:
            line = line[:177] + "…"
        extra = len(line) + (1 if lines else 0)
        if used + extra > limit:
            break
        lines.append(line)
        used += extra
    return "\n".join(lines)
