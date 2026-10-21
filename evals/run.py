"""Точка входа: python evals/run.py [--only категория] [--json отчёт.json]

Логика — в evals/runner.py, задачи — в evals/tasks/*.yaml.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.runner import main                                    # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
