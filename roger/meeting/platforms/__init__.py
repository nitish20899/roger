"""Importing this package registers every platform Roger can drive."""
from . import google_meet, teams  # noqa: F401

__all__ = ["google_meet", "teams"]
