# Headroom Gateway - Implementation Plan

## Goal

Build a lightweight, stateless OpenAI-compatible gateway that:

1. Accepts OpenAI Chat Completions requests
2. Applies Headroom context compression
3. Routes requests to a configured model/provider
4. Streams responses back to the client
5. Requires **no database**
6. Runs as a single container/process
7. Works with Claude Code, OpenCode, Cline, Aider, Roo Code, Hermes, PydanticAI, LangGraph, etc.

---

# High-Level Architecture

```text
Client -> Headroom Gateway -> Provider
```

## Technology Stack

- Python 3.12+
- FastAPI
- Uvicorn
- httpx
- pydantic
- headroom

No Postgres, Redis, Kafka, or Celery.

---

## Project Structure

```text
headroom-gateway/
├── app.py
├── config.py
├── adapters/
├── middleware/
├── routes/
├── tests/
├── Dockerfile
└── README.md
```

---

## Implementation Phases

### Phase 1 - MVP

- Implement `/v1/chat/completions`
- Validate requests
- Forward to provider
- Return OpenAI-compatible response

### Phase 2 - Model Routing

- Alias logical models to providers
- Environment-based configuration

### Phase 3 - Headroom Integration

- Compress when context exceeds threshold
- Capture compression metrics

### Phase 4 - Provider Adapters

- OpenAI
- Anthropic
- Gemini
- Ollama
- OpenRouter

### Phase 5 - Streaming

- Support SSE streaming
- Pass chunks through without buffering

### Phase 6 - Authentication

- Gateway API key
- Optional multi-key support

### Phase 7 - Observability

- /health
- /metrics
- latency and compression tracking

### Phase 8 - Dockerization

- Single container deployment
- Stateless operation

---

## Future Enhancements

- Fallback routing
- Cost-aware routing
- SQLite/DuckDB cache
- Semantic compression cache

---

## End State

A lightweight, stateless, OpenAI-compatible gateway that performs Headroom compression and routes requests across multiple LLM providers without requiring dedicated infrastructure.