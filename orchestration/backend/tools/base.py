#  Orchestration Engine - Tool Base Class
#
#  Abstract base class for tools available to Claude task instances.
#
#  Depends on: (none)
#  Used by:    tools/registry.py, tools/*

from abc import ABC, abstractmethod


class Tool(ABC):
    """Base class for tools that Claude task instances can call."""

    name: str = ""
    description: str = ""
    parameters: dict  # JSON Schema for input — subclasses must define

    @abstractmethod
    async def execute(self, params: dict) -> str:
        """Execute the tool and return a text result."""
        ...

    def to_claude_tool(self) -> dict:
        """Convert to Anthropic API tool definition format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
