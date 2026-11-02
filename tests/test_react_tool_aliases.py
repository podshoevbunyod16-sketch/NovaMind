def test_public_react_tool_names_are_registered_in_the_single_registry():
    import tool_registry
    tool_registry.bootstrap_default_tools()
    names = {spec.name for spec in tool_registry.list_tools()}
    assert {"web_search", "web_fetch", "file_read", "file_write", "file_edit",
            "file_list", "file_search", "bash", "python", "image_generate",
            "ask_user"}.issubset(names)
    schemas = {item["name"]: item for item in tool_registry.tool_schemas()}
    assert schemas["web_fetch"]["risk"] == "safe"
    assert schemas["image_generate"]["risk"] == "write"
