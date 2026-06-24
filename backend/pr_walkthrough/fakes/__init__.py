"Fake adapter implementations backed by fixtures. Default wiring for standalone demo."

from .pr_source import FakePRSource
from .llm import FakeLLM
from .tts import FakeTTS
from .stt import FakeSTT
from .context import FakeContext

__all__ = ["FakePRSource", "FakeLLM", "FakeTTS", "FakeSTT", "FakeContext"]
