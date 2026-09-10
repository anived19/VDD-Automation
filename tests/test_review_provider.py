"""Unit tests for provider-selection logic in vdd/review/model.py.
Pure env-var logic only -- no network calls, no real LLM server."""
import pytest

from vdd.review.model import select_provider

_ENV_VARS = ("LLM_PROVIDER", "GEMINI_API_KEY", "OPENAI_API_KEY", "QWEN_BASE_URL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_no_keys_returns_none():
    assert select_provider() is None


def test_gemini_key_alone(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert select_provider() == "gemini"


def test_qwen_base_url_alone(monkeypatch):
    monkeypatch.setenv("QWEN_BASE_URL", "http://localhost:8000/v1")
    assert select_provider() == "qwen"


def test_openai_key_alone(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert select_provider() == "openai"


def test_explicit_llm_provider_qwen_without_base_url_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    assert select_provider() is None


def test_explicit_llm_provider_qwen_with_base_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("QWEN_BASE_URL", "http://localhost:8000/v1")
    assert select_provider() == "qwen"


def test_gemini_takes_priority_when_no_explicit_provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("QWEN_BASE_URL", "http://localhost:8000/v1")
    assert select_provider() == "gemini"


def test_unrecognized_explicit_provider_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "not-a-real-provider")
    with pytest.raises(ValueError):
        select_provider()
