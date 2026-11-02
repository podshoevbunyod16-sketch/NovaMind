from agent.runtime import parse_bool, request_tools_allowed


def test_agent_request_flags_parse_json_strings_safely():
    assert not request_tools_allowed({"tools": "false"})
    assert not request_tools_allowed({"auto_search": "off"})
    assert request_tools_allowed({"tools": "true"})
    assert request_tools_allowed({})
    assert not parse_bool("false", default=True)
    assert not parse_bool("0", default=True)
    assert parse_bool("yes", default=False)
    assert not parse_bool(None, default=False)
