"""
brainctl — A cognitive memory system for AI agents.

Quick start:
    from agentmemory import Brain

    brain = Brain()
    brain.remember("User prefers dark mode")
    brain.search("preferences")
"""

__version__ = "2.4.9"

from agentmemory.brain import Brain

__all__ = ["Brain", "__version__"]
