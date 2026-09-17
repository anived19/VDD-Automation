"""trace.py must log a reviewer's thoughts whichever provider produced them.

Gemini (include_thoughts=True) puts them in content as {"type": "thinking"}
blocks; OpenAI (reasoning={"summary": "auto"}, output_version="responses/v1")
as {"type": "reasoning", "summary": [...]} items. Both go through
langchain-core's content_blocks, which is exercised here with hand-built
AIMessages -- no network, no key.

Run: C:\Python313\python.exe -m pytest tests -q
"""
from langchain_core.messages import AIMessage, ToolMessage

from vdd.review.trace import extract_pass_usage, serialize_messages, summarize_usage


def test_gemini_thinking_blocks_are_logged():
    m = AIMessage(content=[{"type": "thinking", "thinking": "check the bank row"},
                           {"type": "text", "text": "done"}],
                  response_metadata={"model_provider": "google_genai"})
    out = serialize_messages([m])[0]
    assert out == {"type": "ai", "content": "done", "thinking": "check the bank row", "tool_calls": []}


def test_gemini_thinking_blocks_are_logged_without_provider_metadata():
    m = AIMessage(content=[{"type": "thinking", "thinking": "raw shape"}, {"type": "text", "text": "done"}])
    out = serialize_messages([m])[0]
    assert out["thinking"] == "raw shape" and out["content"] == "done"


def test_openai_reasoning_summaries_are_logged():
    m = AIMessage(content=[{"type": "reasoning", "id": "rs_1",
                            "summary": [{"type": "summary_text", "text": "first thought"},
                                        {"type": "summary_text", "text": "second thought"}]},
                           {"type": "text", "text": "done", "id": "msg_1"}],
                  response_metadata={"model_provider": "openai", "output_version": "responses/v1"})
    out = serialize_messages([m])[0]
    assert out["thinking"] == "first thought\nsecond thought" and out["content"] == "done"


def test_plain_string_and_tool_call_messages_still_serialize():
    ai = AIMessage(content="", tool_calls=[{"name": "recheck_pep", "id": "c1", "args": {"person_names": ["X"]}}])
    tool = ToolMessage(content='{"ok": true}', tool_call_id="c1", name="recheck_pep")
    plain = AIMessage(content="just text")
    ai_out, tool_out, plain_out = serialize_messages([ai, tool, plain])
    assert ai_out["content"] is None and ai_out["thinking"] is None
    assert ai_out["tool_calls"] == [{"name": "recheck_pep", "args": {"person_names": ["X"]}}]
    assert tool_out == {"type": "tool_result", "tool_name": "recheck_pep", "content": '{"ok": true}'}
    assert plain_out["content"] == "just text" and plain_out["thinking"] is None


def test_reasoning_tokens_are_summed_and_default_to_zero():
    with_reasoning = AIMessage(content="a", usage_metadata={
        "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
        "output_token_details": {"reasoning": 30}})
    without = AIMessage(content="b", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    p = extract_pass_usage([with_reasoning, without, ToolMessage(content="x", tool_call_id="t")])
    assert p == {"input_tokens": 110, "output_tokens": 55, "total_tokens": 165,
                 "reasoning_tokens": 30, "llm_call_count": 2}
    assert summarize_usage([p, p])["reasoning_tokens"] == 60
    assert summarize_usage([{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "llm_call_count": 1}]
                           )["reasoning_tokens"] == 0                  # older per-pass dicts lack the key
