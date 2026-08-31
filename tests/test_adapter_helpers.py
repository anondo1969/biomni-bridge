from biomni_bridge.adapter import _clean_fallback_answer, _extract_solution


def test_extract_solution():
    text = "thinking <solution>Final answer here</solution>"
    assert _extract_solution(text) == "Final answer here"


def test_clean_fallback_answer_removes_execution_blocks():
    text = "<execute>print('x')</execute><observation>x</observation>Useful summary"
    assert _clean_fallback_answer(text) == "Useful summary"


def test_clean_fallback_answer_drops_think_content():
    text = "<think>private reasoning</think>Useful summary"
    assert _clean_fallback_answer(text) == "Useful summary"


def test_extract_solution_drops_nested_think_content():
    text = "<solution><think>private reasoning</think>Final answer</solution>"
    assert _extract_solution(text) == "Final answer"
