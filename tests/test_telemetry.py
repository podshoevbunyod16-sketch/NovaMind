"""Сквозная трассировка: спаны, сохранение, поиск, метрики, редакция секретов."""
import json
import time

import telemetry


def _tracer(tmp_path):
    return telemetry.Tracer(directory=str(tmp_path / "traces"))


def test_trace_records_spans_and_durations(tmp_path):
    tracer = _tracer(tmp_path)
    trace = tracer.start("agent.turn", run_id="run-1", chat_id="chat-1")
    with telemetry.span_context(trace, "tool:web_search", kind="tool") as span:
        span.set_attribute("results", 3)
        time.sleep(0.005)
    trace.finish("ok", {"steps": 1})

    record = trace.as_dict()
    assert record["trace_id"] == trace.trace_id
    assert record["status"] == "ok"
    assert record["result"]["steps"] == 1
    names = [span["name"] for span in record["spans"]]
    assert "turn" in names and "tool:web_search" in names
    tool_span = next(span for span in record["spans"] if span["name"] == "tool:web_search")
    assert tool_span["kind"] == "tool"
    assert tool_span["duration_ms"] >= 3
    assert record["summary"]["tools"] == {"tool:web_search": 1}


def test_trace_is_saved_and_loaded(tmp_path):
    tracer = _tracer(tmp_path)
    trace = tracer.start("agent.turn", run_id="run-42", chat_id="c-1")
    trace.finish("ok", {"steps": 2})
    path = tracer.save(trace)
    assert path.endswith(".jsonl")

    loaded = tracer.get(trace.trace_id)
    assert loaded and loaded["trace_id"] == trace.trace_id
    assert loaded["result"]["steps"] == 2
    # отдельный файл на трассу — чтобы не читать весь журнал за день
    assert (tmp_path / "traces" / f"{trace.trace_id}.json").is_file()


def test_find_by_run_id(tmp_path):
    tracer = _tracer(tmp_path)
    trace = tracer.start("agent.turn", run_id="abc123")
    tracer.save(trace.finish("ok"))
    found = tracer.find_by_run("abc123")
    assert found and found["trace_id"] == trace.trace_id
    assert tracer.find_by_run("nope") is None


def test_secrets_are_redacted_in_attributes(tmp_path):
    tracer = _tracer(tmp_path)
    trace = tracer.start("agent.turn", api_key="sk-abcdefghijklmnopqrstuvwxyz", chat_id="c")
    trace.set_attribute("authorization", "Bearer abcdef")
    span = trace.span("call", attributes={"token": "ghp_abcdefghijklmnopqrstuvwxyz",
                                          "safe": "visible"})
    span.finish()
    record = trace.finish("ok").as_dict()
    assert record["attributes"]["api_key"] == "[скрыто]"
    assert record["attributes"]["authorization"] == "[скрыто]"
    assert span.as_dict()["attributes"]["token"] == "[скрыто]"
    assert span.as_dict()["attributes"]["safe"] == "visible"


def test_secret_values_inside_text_are_masked(tmp_path):
    tracer = _tracer(tmp_path)
    trace = tracer.start("agent.turn", goal="мой ключ gsk_abcdefghijklmnopqrstuvwx")
    record = trace.finish("ok").as_dict()
    assert "gsk_abcdefghijklmnopqrstuvwx" not in json.dumps(record, ensure_ascii=False)


def test_span_errors_are_marked(tmp_path):
    tracer = _tracer(tmp_path)
    trace = tracer.start("agent.turn")
    try:
        with telemetry.span_context(trace, "tool:shell", kind="tool"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    record = trace.finish("error").as_dict()
    span = next(item for item in record["spans"] if item["name"] == "tool:shell")
    assert span["status"] == "error"
    assert span["events"][0]["name"] == "exception"
    assert record["summary"]["errors"] == 1


def test_metrics_aggregate_recent_traces(tmp_path):
    tracer = _tracer(tmp_path)
    for index in range(4):
        trace = tracer.start("agent.turn", provider="groq")
        span = trace.span("tool:web_search", kind="tool")
        span.finish()
        tracer.save(trace.finish("ok" if index else "error"))
    metrics = tracer.metrics()
    assert metrics["turns"] == 4
    assert metrics["errors"] == 1
    assert metrics["error_rate"] == 0.25
    assert metrics["tools"]["tool:web_search"] == 4


def test_recent_returns_newest_first(tmp_path):
    tracer = _tracer(tmp_path)
    first = tracer.start("agent.turn", run_id="1")
    tracer.save(first.finish("ok"))
    second = tracer.start("agent.turn", run_id="2")
    tracer.save(second.finish("ok"))
    recent = tracer.recent(5)
    assert recent[0]["trace_id"] == second.trace_id


def test_disabled_tracer_does_not_write(tmp_path):
    tracer = telemetry.Tracer(directory=str(tmp_path / "traces"), enabled_flag=False)
    trace = tracer.start("agent.turn", run_id="x")
    trace.finish("ok")
    assert tracer.save(trace) == ""
    assert not (tmp_path / "traces").exists()


def test_get_rejects_bad_trace_id(tmp_path):
    tracer = _tracer(tmp_path)
    assert tracer.get("../../etc/passwd") is None
    assert tracer.get("") is None


def test_summarize_marks_slowest_span():
    spans = [{"name": "a", "kind": "tool", "status": "ok", "duration_ms": 5},
             {"name": "b", "kind": "step", "status": "ok", "duration_ms": 50}]
    summary = telemetry.summarize_spans(spans)
    assert summary["slowest"]["name"] == "b"
    assert summary["spans"] == 2


def test_summary_reports_configuration():
    data = telemetry.summary()
    assert "enabled" in data and "directory" in data
