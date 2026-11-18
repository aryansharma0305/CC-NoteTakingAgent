"""Shared contracts for the video note-taking agent.

Everything in this package is frozen: gateway, worker, notion_publisher and the
extension (which mirrors errors.py's user-facing copy) all depend on it, so any
change goes through a reviewed PR (see .github/CODEOWNERS).
"""
