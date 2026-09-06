from importlib.metadata import version


def test_agent_runtime_dependencies_are_frozen() -> None:
    """Agent 打包使用本实现计划审查过的版本。"""

    assert version("langchain-core") == "1.6.0"
    assert version("openai") == "3.6.0"
    assert version("langgraph") == "1.2.11"
