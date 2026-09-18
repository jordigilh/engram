from spike.response_shaping import _compact_json_whitespace, _shape_tool_result


def test_compacts_json_whitespace_without_rewriting_values():
    raw = ' \n { "a": "x  y", "a": 1, "number": 1.00, "items": [ true, null ] } \t'

    assert _compact_json_whitespace(raw) == '{"a":"x  y","a":1,"number":1.00,"items":[true,null]}'


def test_invalid_json_and_multiple_values_pass_through():
    invalid = '{ "a": 1 '
    multiple = '{ "a": 1 } { "b": 2 }'

    assert _compact_json_whitespace(invalid) == invalid
    assert _compact_json_whitespace(multiple) == multiple


def test_shapes_only_structured_json_backends():
    result = {
        "content": [{"type": "text", "text": '{ "text": "keep  spaces", "n": 1 }'}],
        "isError": False,
    }

    shaped, strategy = _shape_tool_result("docs", result)

    assert strategy == "json-whitespace"
    assert shaped["content"][0]["text"] == '{"text":"keep  spaces","n":1}'
    assert result["content"][0]["text"] == '{ "text": "keep  spaces", "n": 1 }'


def test_code_search_and_errors_remain_unchanged():
    result = {
        "content": [{"type": "text", "text": '{ "code": "keep" }'}],
        "isError": False,
    }
    error = {"content": [{"type": "text", "text": '{ "error": true }'}], "isError": True}

    shaped_code, code_strategy = _shape_tool_result("code", result)
    shaped_error, error_strategy = _shape_tool_result("docs", error)

    assert shaped_code == result
    assert code_strategy == "none"
    assert shaped_error == error
    assert error_strategy == "none"
