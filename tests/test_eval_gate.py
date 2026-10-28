"""Гейт качества: метрики прогона, сравнение с базовой линией, коды выхода."""
import json

import pytest

from evals import gate


def _result(task_id, ok=True, category="search", steps=2, tokens=100, time_=1.0):
    return {"id": task_id, "category": category, "ok": ok,
            "steps": steps, "tokens": tokens, "time": time_,
            "reason": "" if ok else "пустой ответ"}


def test_summarize_counts_and_categories():
    results = [_result("a"), _result("b", ok=False), _result("c", category="safety")]
    summary = gate.summarize(results)
    assert summary["total"] == 3
    assert summary["passed"] == 2
    assert summary["pass_rate"] == pytest.approx(0.667, abs=0.001)
    assert summary["categories"]["search"]["passed"] == 1
    assert summary["failed_ids"] == ["b"]


def test_summarize_handles_empty_run():
    summary = gate.summarize([])
    assert summary["total"] == 0
    assert summary["pass_rate"] == 0.0


def test_gate_passes_good_run():
    current = gate.summarize([_result("a"), _result("b")])
    decision = gate.compare(current, None)
    assert decision["ok"] is True
    assert decision["verdict"] == "pass"


def test_gate_fails_low_pass_rate():
    current = gate.summarize([_result("a"), _result("b", ok=False)])
    decision = gate.compare(current, None, {"min_pass_rate": 0.9})
    assert decision["ok"] is False
    assert decision["violations"][0]["rule"] == "min_pass_rate"


def test_gate_detects_new_failures_against_baseline():
    baseline_results = [_result("a"), _result("b")]
    current_results = [_result("a"), _result("b", ok=False)]
    decision = gate.compare(gate.summarize(current_results),
                            gate.summarize(baseline_results),
                            {"max_new_failures": 0, "max_pass_rate_drop": 1.0},
                            baseline_results)
    assert decision["ok"] is False
    assert decision["new_failures"] == ["b"]
    assert any(item["rule"] == "max_new_failures" for item in decision["violations"])


def test_gate_detects_token_and_step_regressions():
    baseline_results = [_result("a", steps=2, tokens=100, time_=1.0)]
    current_results = [_result("a", steps=10, tokens=1000, time_=9.0)]
    decision = gate.compare(gate.summarize(current_results),
                            gate.summarize(baseline_results),
                            {"max_pass_rate_drop": 1.0},
                            baseline_results)
    rules = {item["rule"] for item in decision["violations"]}
    assert {"max_steps_increase", "max_tokens_increase", "max_time_increase"} <= rules


def test_gate_enforces_safety_category():
    results = [_result("s1", ok=False, category="safety"), _result("a")]
    decision = gate.compare(gate.summarize(results), None,
                            {"min_pass_rate": 0.0,
                             "min_category_pass_rate": {"safety": 1.0}})
    assert decision["ok"] is False
    assert any(item["rule"] == "min_category_pass_rate"
               for item in decision["violations"])


def test_gate_warns_about_missing_category():
    decision = gate.compare(gate.summarize([_result("a")]), None,
                            {"min_category_pass_rate": {"safety": 1.0}})
    assert decision["ok"] is True
    assert decision["warnings"]


def test_gate_rejects_empty_run():
    decision = gate.compare(gate.summarize([]), None, {"min_pass_rate": 0.0})
    assert decision["ok"] is False
    assert any(item["rule"] == "no_tasks" for item in decision["violations"])


def test_gate_report_from_files(tmp_path):
    report = tmp_path / "report.json"
    baseline = tmp_path / "baseline.json"
    report.write_text(json.dumps([_result("a"), _result("b")]), encoding="utf-8")
    baseline.write_text(json.dumps([_result("a"), _result("b")]), encoding="utf-8")
    decision = gate.gate_report(str(report), str(baseline))
    assert decision["ok"] is True
    assert decision["baseline"]["total"] == 2


def test_format_decision_mentions_regressions():
    current = gate.summarize([_result("a", ok=False)])
    decision = gate.compare(current, None, {"min_pass_rate": 0.9})
    text = gate.format_decision(decision)
    assert "Регрессия" in text
    assert "min_pass_rate" not in text          # человекочитаемое сообщение
    assert "ниже порога" in text


def test_update_baseline_writes_report(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_result("a")]), encoding="utf-8")
    target = tmp_path / "baseline.json"
    info = gate.update_baseline(str(report), str(target))
    assert info["tasks"] == 1
    assert info["pass_rate"] == 1.0
    assert json.loads(target.read_text(encoding="utf-8"))[0]["id"] == "a"


def test_cli_returns_zero_for_good_run(tmp_path, capsys):
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_result("a"), _result("b")]), encoding="utf-8")
    assert gate.main(["--report", str(report)]) == 0
    assert "Регрессий нет" in capsys.readouterr().out


def test_cli_returns_one_for_regression(tmp_path, capsys):
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_result("a", ok=False)]), encoding="utf-8")
    assert gate.main(["--report", str(report)]) == 1
    assert "Регрессия" in capsys.readouterr().out


def test_cli_writes_decision_json(tmp_path):
    report = tmp_path / "report.json"
    out = tmp_path / "decision.json"
    report.write_text(json.dumps([_result("a")]), encoding="utf-8")
    assert gate.main(["--report", str(report), "--json", str(out)]) == 0
    decision = json.loads(out.read_text(encoding="utf-8"))
    assert decision["ok"] is True


def test_cli_requires_existing_report(capsys):
    assert gate.main(["--report", "/nonexistent.json"]) == 2
    assert "Нужен существующий отчёт" in capsys.readouterr().out


def test_thresholds_file_is_valid():
    thresholds = gate.load_thresholds()
    assert 0 < float(thresholds["min_pass_rate"]) <= 1
    assert "safety" in thresholds["min_category_pass_rate"]


# ══════════════ офлайн-прогон не приговор ══════════════

def _offline_result(task_id, ok=False, category="search"):
    item = _result(task_id, ok=ok, category=category)
    item["offline"] = True
    item["provider"] = "local_demo"
    return item


def test_summarize_counts_offline_share():
    summary = gate.summarize([_offline_result("a"), _offline_result("b"), _result("c", ok=True)])
    assert summary["offline"] == 2
    assert summary["offline_share"] == pytest.approx(0.667, abs=0.001)


def test_gate_skips_offline_run():
    current = gate.summarize([_offline_result("a"), _offline_result("b"),
                              _offline_result("c", ok=True)])
    decision = gate.compare(current, None)
    assert decision["skipped"] is True
    assert decision["verdict"] == "skipped"
    assert decision["ok"] is False
    assert "без модели" in decision["skip_reason"]
    assert any(w["rule"] == "offline_run" for w in decision["warnings"])


def test_gate_judges_offline_run_when_asked():
    current = gate.summarize([_offline_result("a"), _offline_result("b")])
    decision = gate.compare(current, None, judge_offline=True)
    assert decision["skipped"] is False
    assert decision["verdict"] == "regression"


def test_gate_does_not_skip_mostly_online_run():
    results = [_result("a", ok=True), _result("b", ok=True), _result("d", ok=True),
               _result("e", ok=True), _offline_result("c")]
    decision = gate.compare(gate.summarize(results), None)
    assert decision["skipped"] is False
    assert decision["verdict"] == "pass"


def test_cli_returns_two_for_offline_run(tmp_path, capsys):
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_offline_result("a"), _offline_result("b", ok=True)]),
                      encoding="utf-8")
    assert gate.main(["--report", str(report)]) == 2
    assert "Пороги не применялись" in capsys.readouterr().out


def test_cli_judge_offline_flag(tmp_path, capsys):
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_offline_result("a"), _offline_result("b")]),
                      encoding="utf-8")
    assert gate.main(["--report", str(report), "--judge-offline"]) == 1
    assert "Регрессия" in capsys.readouterr().out
