"""Tests for generate-dashboard.py's "Recent Correction Patterns" section
(added 2026-08-04 alongside nightly-learn.py's reflect_windowed()) -- see
that function's docstring and docs/findings/2026-08.md for why a
time-windowed view is surfaced separately from the all-time reflect_result.
"""
from __future__ import annotations


def _base_report(**overrides):
    report = {
        "date": "2026-08-04",
        "effectiveness": {},
        "bank_stats": {},
        "triage": {},
    }
    report.update(overrides)
    return report


class TestRecentCorrectionPatternsSection:
    def test_no_windowed_key_omits_section_entirely(self, generate_dashboard):
        """Older daily reports predate reflect_windowed_7d -- must not crash
        or print a bogus empty section for them."""
        out = generate_dashboard.generate_dashboard([_base_report()])

        assert "Recent Correction Patterns" not in out

    def test_zero_corrections_in_window_shows_positive_signal(self, generate_dashboard):
        report = _base_report(reflect_windowed_7d={
            "window_days": 7, "corrections_in_window": 0, "result": None,
        })

        out = generate_dashboard.generate_dashboard([report])

        assert "Recent Correction Patterns (last 7 days)" in out
        assert "**0** correction(s) retained in this window." in out
        assert "genuinely positive signal" in out

    def test_result_text_is_rendered(self, generate_dashboard):
        report = _base_report(reflect_windowed_7d={
            "window_days": 7,
            "corrections_in_window": 3,
            "result": {"text": "Top pattern: skipping RED phase."},
        })

        out = generate_dashboard.generate_dashboard([report])

        assert "**3** correction(s) retained in this window." in out
        assert "Top pattern: skipping RED phase." in out

    def test_error_is_surfaced_instead_of_crashing(self, generate_dashboard):
        report = _base_report(reflect_windowed_7d={
            "window_days": 7,
            "corrections_in_window": 0,
            "result": None,
            "error": "connection refused",
        })

        out = generate_dashboard.generate_dashboard([report])

        assert "Windowed reflect failed: connection refused" in out
