from importlib.metadata import version


def test_agent_runtime_dependencies_are_frozen() -> None:
    """Agent packaging uses the versions reviewed with this implementation plan."""

    assert version("langchain-core") == "1.6.0"
    assert version("langchain-openai") == "1.6.0"
    assert version("langgraph") == "1.2.11"
