"""
brainctl — A cognitive memory system for AI agents.

Quick start:
    from brainctl import Brain
    
    brain = Brain()
    brain.remember("User prefers dark mode")
    brain.search("preferences")
"""

__version__ = "1.0.0"

from agentmemory.brain import Brain

__all__ = ["Brain", "__version__"]
