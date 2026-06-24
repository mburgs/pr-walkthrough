# LLM Adapter — Stream 3

Claude-backed implementation of the `LLMAdapter` protocol.

## Setup

```bash
cd backend
pip install -e ".[dev]"
# or: pip install anthropic pydantic pytest pytest-asyncio
```

## CLI commands

All commands run from the `backend/` directory.

### Mock mode (no API key needed)

Prints fixture data verbatim — useful for integration testing without the API.

```bash
python -m pr_walkthrough.llm.cli --mock plan ../fixtures/pr_small
python -m pr_walkthrough.llm.cli --mock narrate ../fixtures/pr_small c1
python -m pr_walkthrough.llm.cli --mock narrate ../fixtures/pr_small c2
python -m pr_walkthrough.llm.cli --mock answer ../fixtures/pr_small c2 "Is rotate() atomic?"
```

### Real Claude (requires ANTHROPIC_API_KEY)

```bash
export ANTHROPIC_API_KEY=sk-ant-...

python -m pr_walkthrough.llm.cli plan ../fixtures/pr_small
python -m pr_walkthrough.llm.cli narrate ../fixtures/pr_small c1
python -m pr_walkthrough.llm.cli answer ../fixtures/pr_small c2 "Does rotate share the DB connection?"
```

## Tests

```bash
cd backend
# Mock tests only (no API key needed):
pytest tests/ -v -m "not live"

# Include live tests (requires ANTHROPIC_API_KEY):
pytest tests/ -v
```

## Streaming API

`narrate_chunk` satisfies the `LLMAdapter` Protocol (non-streaming). For token streaming:

```python
narration_result, token_stream = await adapter.narrate_chunk_streaming(plan, chunk, related)
async for token in token_stream:
    await sse_queue.put(NarrationTokenEvent(chunk_id=chunk.chunk_id, text=token))
narration = token_stream.get_result()  # ChunkNarration, valid after stream exhausted
```

## Token cost estimates (pr_small fixture, ~50 LOC diff)

| Call | Model | Est. input tokens | Est. output tokens | Est. cost (cached) |
|------|-------|------------------|--------------------|-------------------|
| `plan_tour` | claude-opus-4-7 | ~2 000 | ~1 500 | ~$0.01 |
| `narrate_chunk` | claude-sonnet-4-6 | ~3 000* | ~500 | ~$0.003* |
| `answer_follow_up` | claude-sonnet-4-6 | ~2 000* | ~400 | ~$0.002* |

\* Per-call cost after cache hits on the system prompt + diff context block.  
For a 10-file, 400-LOC PR, expect 3–4× higher input tokens; narrate_chunk costs dominate.

Cache hit rates are high within a session (system prompt + full diff cached after first narrate_chunk call), reducing input costs to ~10% of uncached for repeated chunks.
