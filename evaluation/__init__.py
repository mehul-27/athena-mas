"""Athena evaluation harness: dataset, experiment config, runner, scoring, analysis.

This package is deliberately outside ``athena/``: it is measurement code, not
product code. Nothing in here may be imported by the pipeline, and nothing in
here invents a result. A run that did not happen leaves no record.

Entry points::

    python -m evaluation.runner --dry-run
    python -m evaluation.runner --out evaluation/results/<timestamp>
    python -m evaluation.analyze --results evaluation/results/<timestamp>
    python -m evaluation.report
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
