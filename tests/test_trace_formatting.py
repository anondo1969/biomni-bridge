import pytest

from biomni_bridge.adapter import format_trace_markdown


@pytest.mark.parametrize(
    "snippet",
    [
        "if x < 5 and y > 3:",
        "if a <= b >= c: pass",
        "df = df[df.score > 0.5]",
        "mask = (a >= 1) & (b <= 2)",
    ],
)
def test_execute_code_preserves_comparisons(snippet):
    out = format_trace_markdown(f"<execute>\n{snippet}\n</execute>")
    assert snippet in out
    assert "```python" in out


def test_plain_inequalities_are_visible_and_not_html():
    out = format_trace_markdown("We found p < 0.05 and fold-change > 2.")
    assert "p &lt; 0.05" in out
    assert "fold-change &gt; 2" in out


def test_observation_becomes_text_fence():
    out = format_trace_markdown("<observation>n > 5, p < 0.01</observation>")
    assert "```text" in out
    assert "n > 5, p < 0.01" in out
    assert "<observation>" not in out


def test_solution_is_labelled_and_raw_html_is_escaped():
    out = format_trace_markdown("<solution>**Result** <script>alert(1)</script></solution>")
    assert "**Solution**" in out
    assert "**Result**" in out
    assert "&lt;script&gt;" in out
    assert "<script>" not in out


def test_unclosed_execute_still_gets_a_valid_fence():
    out = format_trace_markdown("<execute>\nprint('still running')")
    assert out.count("```") == 2
    assert "<execute>" not in out


def test_fence_expands_around_embedded_triple_backticks():
    out = format_trace_markdown("<execute>\ntext = '```'\n</execute>")
    assert "````python" in out
    assert out.count("````") == 2


def test_think_block_content_is_not_exposed():
    out = format_trace_markdown("before <think>private chain</think> after")
    assert "private chain" not in out
    assert "before" in out and "after" in out


def test_unknown_markup_is_escaped():
    out = format_trace_markdown("<b>not trusted html</b>")
    assert "&lt;b&gt;not trusted html&lt;/b&gt;" in out
