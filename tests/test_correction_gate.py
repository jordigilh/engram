"""Tests for correction_gate.py -- the shared Haiku correction-detection gate
used by both nightly-learn.py and cocoindex-flows.py.
"""
from __future__ import annotations

import json
import re

import pytest

import correction_gate as cg


@pytest.fixture(autouse=True)
def reset_disk_cache(tmp_path, monkeypatch):
    """classify_cached() memoizes in a module-level global on top of the disk
    file -- both must be reset per test, or an earlier test's cache entry
    leaks into a later one."""
    monkeypatch.setattr(cg, "CACHE_PATH", tmp_path / "correction-cache.json")
    monkeypatch.setattr(cg, "_cache", None)


class TestIsSystemBoilerplate:
    def test_matches_known_prefix(self):
        text = "Briefly inform the user about the task result and perform any follow-up actions as needed."
        assert cg.is_system_boilerplate(text) is True

    def test_matches_subagent_visibility_prefix(self):
        text = "The beginning of the above subagent result is already visible to the user. Do not repeat it."
        assert cg.is_system_boilerplate(text) is True

    def test_matches_wrapper_tag(self):
        assert cg.is_system_boilerplate("<system_reminder>some content</system_reminder>") is True
        assert cg.is_system_boilerplate("<mcp_server_catalog>...</mcp_server_catalog>") is True

    def test_matches_wrapper_tag_with_leading_whitespace(self):
        assert cg.is_system_boilerplate("   <attached_files>foo</attached_files>") is True

    def test_normal_correction_text_is_not_boilerplate(self):
        assert cg.is_system_boilerplate("that's not how we do it, we don't use HAPI here") is False

    def test_empty_string_is_not_boilerplate(self):
        assert cg.is_system_boilerplate("") is False


class TestDetectorMode:
    def test_default_is_haiku(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_CORRECTION_DETECTOR", raising=False)
        assert cg.detector_mode() == "haiku"

    def test_explicit_regex(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CORRECTION_DETECTOR", "regex")
        assert cg.detector_mode() == "regex"

    def test_explicit_haiku_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CORRECTION_DETECTOR", "HAIKU")
        assert cg.detector_mode() == "haiku"

    def test_invalid_value_falls_back_to_haiku(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CORRECTION_DETECTOR", "bogus")
        assert cg.detector_mode() == "haiku"


class TestIsCorrection:
    def test_empty_text_is_false(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CORRECTION_DETECTOR", "regex")
        assert cg.is_correction("", [re.compile(".*")]) is False

    def test_text_over_2000_chars_is_always_false(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CORRECTION_DETECTOR", "regex")
        long_text = "we don't use HAPI here " * 100
        assert len(long_text) > 2000
        assert cg.is_correction(long_text, [re.compile("HAPI")]) is False

    def test_boilerplate_is_always_false_even_if_classifier_would_say_true(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_CORRECTION_DETECTOR", raising=False)
        monkeypatch.setattr(cg, "classify_cached", lambda text: True)
        assert cg.is_correction("<system_reminder>anything</system_reminder>", []) is False

    def test_regex_mode_matches_pattern(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CORRECTION_DETECTOR", "regex")
        patterns = [re.compile(r"we don't use", re.IGNORECASE)]
        assert cg.is_correction("we don't use HAPI here", patterns) is True

    def test_regex_mode_no_match(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CORRECTION_DETECTOR", "regex")
        patterns = [re.compile(r"we don't use", re.IGNORECASE)]
        assert cg.is_correction("please add a new endpoint", patterns) is False

    def test_haiku_mode_delegates_to_classify_cached(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_CORRECTION_DETECTOR", raising=False)
        calls = []
        monkeypatch.setattr(cg, "classify_cached", lambda text: calls.append(text) or True)
        assert cg.is_correction("stop using HAPI, it's deprecated", []) is True
        assert calls == ["stop using HAPI, it's deprecated"]

    def test_haiku_mode_does_not_touch_regex_patterns(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_CORRECTION_DETECTOR", raising=False)
        monkeypatch.setattr(cg, "classify_cached", lambda text: False)
        # regex_patterns intentionally malformed/irrelevant -- must be ignored in haiku mode
        assert cg.is_correction("some text", regex_patterns=None) is False


class TestClassifyCached:
    def test_cache_miss_calls_classify_correction_once_and_persists(self, monkeypatch):
        calls = []

        def fake_classify_correction(text):
            calls.append(text)
            return _fake_result(is_correction=True, category="methodology")

        monkeypatch.setattr(cg, "classify_correction", fake_classify_correction)

        result = cg.classify_cached("we don't use HAPI here")
        assert result is True
        assert calls == ["we don't use HAPI here"]

        # Second call with the same text must hit the disk cache, not re-classify.
        result2 = cg.classify_cached("we don't use HAPI here")
        assert result2 is True
        assert calls == ["we don't use HAPI here"], "expected no second classify_correction call on cache hit"

        assert cg.CACHE_PATH.exists()
        on_disk = json.loads(cg.CACHE_PATH.read_text())
        assert len(on_disk) == 1

    def test_cache_hit_from_preexisting_disk_file_skips_classify_correction(self, monkeypatch):
        import hashlib

        text = "we always write tests first"
        key = hashlib.sha256(text.encode()).hexdigest()
        cg.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cg.CACHE_PATH.write_text(json.dumps({key: {"is_correction": True, "category": "tdd"}}))

        def fail_if_called(text):
            raise AssertionError("classify_correction should not be called on a cache hit")

        monkeypatch.setattr(cg, "classify_correction", fail_if_called)

        assert cg.classify_cached(text) is True

    def test_error_result_is_treated_as_not_a_correction_and_cached_as_such(self, monkeypatch):
        monkeypatch.setattr(
            cg, "classify_correction",
            lambda text: _fake_result(is_correction=True, category=None, error="timeout"),
        )
        # is_correction=True but error is set -> classify_cached must still return False.
        assert cg.classify_cached("some message") is False


class TestSaveCacheConcurrency:
    def test_concurrent_saves_do_not_race_on_the_same_tmp_file(self):
        """Regression test for a real production FileNotFoundError (2026-07-17,
        see docs/FINDINGS.md): two concurrent _save_cache() calls raced on the
        same fixed '.tmp' filename -- one writer's rename() found the file
        already moved away by the other. This can happen across separate
        transcript-app threads within one cocoindex-flows.py process, or
        across the cocoindex-flows.py and nightly-learn.py processes sharing
        the same CACHE_PATH, since threading.Lock() never spans processes.

        Calls _save_cache() directly from multiple threads without holding
        cg._cache_lock, deliberately -- the lock only ever protected the
        single-process case; the fix must make the tmp-file mechanism itself
        collision-free regardless of any Python-level lock.
        """
        import threading

        cg._cache = {}
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(25):
                    cg._cache[f"key-{n}-{i}"] = {"is_correction": True, "category": "x"}
                    cg._save_cache()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"concurrent _save_cache() calls raised: {errors}"
        assert cg.CACHE_PATH.exists()
        json.loads(cg.CACHE_PATH.read_text())  # must always be valid JSON, never a partial write

        leftover_tmp = list(cg.CACHE_PATH.parent.glob(f"{cg.CACHE_PATH.name}.tmp.*"))
        assert leftover_tmp == [], f"leftover tmp files not cleaned up: {leftover_tmp}"


def _fake_result(is_correction: bool, category: str | None, error: str | None = None):
    from types import SimpleNamespace

    return SimpleNamespace(is_correction=is_correction, category=category, error=error)
