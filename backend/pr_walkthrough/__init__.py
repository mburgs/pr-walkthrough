"""pr-walkthrough backend package.

Submodules:
- api/           — FastAPI routes + SSE  (stream 2)
- store/         — SQLite session persistence  (stream 2)
- orchestration/ — coordinates adapters per-session  (stream 2)
- fakes/         — fixture-backed Protocol implementations for dev  (stream 2)
- llm/           — Claude LLMAdapter  (stream 3)
- tts/           — Kokoro/Piper/say TTSAdapter  (stream 4)
- stt/           — Parakeet (MLX) STTAdapter  (stream 5)
- pr/            — gh-CLI PRSource  (stream 6)
- context/       — ripgrep ContextRetriever  (stream 7)
"""
