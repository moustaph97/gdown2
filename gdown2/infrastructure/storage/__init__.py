"""Storage adapters for local disk and resume state."""

from gdown2.infrastructure.storage.fs_sink import FileSystemSink
from gdown2.infrastructure.storage.resume_state import STATE_FILE_NAME, ResumeStateRepository

__all__ = ["FileSystemSink", "ResumeStateRepository", "STATE_FILE_NAME"]

