"""RAG-пайплайн: загрузка, разбиение, BM25-поиск, цитаты, изоляция владельцев."""
import pytest

import knowledge


@pytest.fixture()
def kb(tmp_path):
    return knowledge.KnowledgeBase(str(tmp_path / "knowledge.db"))


DOC = """# Регламент отпусков

Сотрудник подаёт заявление на отпуск за две недели до начала.

## Согласование

Отпуск согласует руководитель отдела. Если руководитель в отпуске, заявление
подписывает заместитель.

## Длительность

Максимальная длительность отпуска без согласования с директором — 14 дней.
"""


def test_ingest_and_stats(kb):
    result = kb.ingest_text("Регламент отпусков", DOC, owner="email:a@b.c",
                            source="hr/policy.md", tags="hr")
    assert result["chunks"] >= 1
    stats = kb.stats(owner="email:a@b.c")
    assert stats["documents"] == 1
    assert stats["chunks"] == result["chunks"]
    assert stats["ready"] is True


def test_reingest_same_text_updates_not_duplicates(kb):
    first = kb.ingest_text("Заметка", "Текст заметки про деплой", owner="u1")
    second = kb.ingest_text("Заметка", "Текст заметки про деплой", owner="u1")
    assert first["id"] == second["id"]
    assert second["updated"] is True
    assert kb.stats(owner="u1")["documents"] == 1


def test_empty_document_is_rejected(kb):
    with pytest.raises(ValueError):
        kb.ingest_text("Пусто", "   ", owner="u1")


def test_search_finds_relevant_chunk(kb):
    kb.ingest_text("Регламент отпусков", DOC, owner="u1")
    hits = kb.search("сколько дней отпуска без согласования", owner="u1")
    assert hits
    assert hits[0].title == "Регламент отпусков"
    assert "14 дней" in hits[0].text


def test_search_prefers_the_right_document(kb):
    kb.ingest_text("Про отпуск", DOC, owner="u1")
    kb.ingest_text("Про Docker", "Docker собирает образы из Dockerfile и запускает "
                                 "контейнеры. Слои кешируются.", owner="u1")
    hits = kb.search("как собрать образ docker", owner="u1")
    assert hits[0].title == "Про Docker"


def test_search_is_case_and_form_insensitive(kb):
    kb.ingest_text("Регламент отпусков", DOC, owner="u1")
    lower = kb.search("отпуска", owner="u1")
    upper = kb.search("ОТПУСК", owner="u1")
    assert lower and upper
    assert lower[0].document_id == upper[0].document_id


def test_owner_isolation(kb):
    kb.ingest_text("Мой документ", "секретный проект касатка", owner="u1")
    assert kb.search("касатка", owner="u1")
    assert kb.search("касатка", owner="u2") == []
    assert kb.search("касатка", owner="", any_owner=True)


def test_context_block_has_citations(kb):
    kb.ingest_text("Регламент отпусков", DOC, owner="u1", source="hr/policy.md")
    block, items = kb.context_block("как согласовать отпуск", owner="u1")
    assert "[K1]" in block
    assert "ВНЕШНИЕ ДАННЫЕ" in block
    assert items and items[0]["ref"] == "K1"
    assert items[0]["source"] == "hr/policy.md"


def test_context_block_empty_without_documents(kb):
    block, items = kb.context_block("что-нибудь", owner="nobody")
    assert block == "" and items == []


def test_context_block_respects_size_limit(kb):
    kb.ingest_text("Большой", "слово " * 4000, owner="u1")
    block, _items = kb.context_block("слово", owner="u1", max_chars=600)
    assert len(block) <= 900


def test_chunking_splits_long_text_and_keeps_overlap():
    text = "\n\n".join(f"Абзац номер {index}. " + "текст " * 120 for index in range(6))
    chunks = knowledge.chunk_text(text, size=300, overlap=40)
    assert len(chunks) > 3
    assert all(len(chunk) <= 400 for chunk in chunks)


def test_chunking_hard_splits_giant_paragraph():
    chunks = knowledge.chunk_text("x" * 2500, size=500, overlap=0)
    assert len(chunks) >= 5
    assert all(len(chunk) <= 500 for chunk in chunks)


def test_tokenize_stems_russian_forms():
    assert knowledge.tokenize("документы") == knowledge.tokenize("документ")
    assert "и" not in knowledge.tokenize("и документ")


def test_ingest_text_file(kb, tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# Заметки\n\nВстреча в среду в 15:00.", encoding="utf-8")
    result = kb.ingest_file(str(path), owner="u1")
    assert result["kind"] == "md"
    assert kb.search("встреча среда", owner="u1")


def test_ingest_csv_file(kb, tmp_path):
    path = tmp_path / "table.csv"
    path.write_text("товар,цена\nкофе,500\nчай,300\n", encoding="utf-8")
    kb.ingest_file(str(path), owner="u1")
    hits = kb.search("цена кофе", owner="u1")
    assert hits and "500" in hits[0].text


def test_ingest_json_file_is_pretty_printed(kb, tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"timeout": 30, "retries": 3}', encoding="utf-8")
    kb.ingest_file(str(path), owner="u1")
    hits = kb.search("timeout", owner="u1")
    assert hits and "timeout" in hits[0].text


def test_missing_file_reports_error(kb):
    with pytest.raises(ValueError):
        kb.ingest_file("/nonexistent/file.pdf", owner="u1")


def test_binary_file_is_rejected(kb, tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ValueError):
        kb.ingest_file(str(path), owner="u1")


def test_documents_and_delete(kb):
    first = kb.ingest_text("Один", "первый документ про кошек", owner="u1")
    second = kb.ingest_text("Два", "второй документ про собак", owner="u1")
    assert {item["id"] for item in kb.documents(owner="u1")} == {first["id"], second["id"]}
    assert kb.delete(first["id"], owner="u1") is True
    assert [item["id"] for item in kb.documents(owner="u1")] == [second["id"]]
    assert kb.search("кошки", owner="u1") == []
    assert kb.search("собаки", owner="u1")


def test_delete_requires_owner(kb):
    document = kb.ingest_text("Мой", "секрет", owner="u1")
    assert kb.delete(document["id"], owner="u2") is False
    assert kb.documents(owner="u1")


def test_search_tool_returns_hint(kb, monkeypatch):
    kb.ingest_text("Регламент отпусков", DOC, owner="u1")
    monkeypatch.setattr(knowledge, "default_base", lambda: kb)
    result = knowledge.search_tool("отпуск", owner="u1")
    assert result["ok"] is True
    assert result["results"]
    assert "[K1]" in result["answer_hint"]


def test_search_tool_without_documents(kb, monkeypatch):
    monkeypatch.setattr(knowledge, "default_base", lambda: kb)
    result = knowledge.search_tool("что-то", owner="u1")
    assert result["ok"] is True
    assert result["results"] == []
    assert "нет подходящих" in result["note"]
