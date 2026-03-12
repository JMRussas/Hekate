# Research 001 — Voice AI Stack (2026-03-08)

Research findings from the voice-enabled ideation assistant design session.

## Speech-to-Text (STT)

| Provider | Type | Latency | Accuracy (WER) | Cost | Notes |
|----------|------|---------|-----------------|------|-------|
| **faster-whisper (local)** | Local | 3000+ WPM on RTX 4090 | ~6.5% English | Free | large-v3-turbo model; not natively streaming; needs WhisperLiveKit or VAD chunking |
| **Deepgram** | Cloud | Sub-300ms streaming | ~8.1% English | Per-minute | Best for real-time streaming priority |
| **AssemblyAI** | Cloud | Moderate | ~5.2% English (best) | ~$0.37/hr | Best accuracy, speaker diarization |
| **Azure Speech** | Cloud | Moderate | Good | $0.017/min | Microsoft ecosystem |
| **Google Speech** | Cloud | Moderate-high | Good | $0.024/min | 100+ languages |

**Decision:** faster-whisper locally on RTX 4090 for zero-cost. Deepgram as cloud fallback.

## Text-to-Speech (TTS)

| Provider | Type | TTFA | Naturalness | Cost | Notes |
|----------|------|------|-------------|------|-------|
| **Cartesia Sonic 3** | Cloud | ~40ms | High | Commercial | Absolute lowest latency |
| **ElevenLabs** | Cloud | ~150ms | Very high | $0.30/1K chars | Most natural, voice cloning |
| **OpenAI TTS** | Cloud | ~200ms | Good | $15/1M chars | Simple integration |
| **Kokoro-82M** | Local | ~97ms | Good (rivals mid-tier) | Free (Apache 2.0) | 82M params, 36x real-time, <$0.06/hr |
| **Coqui XTTS-v2** | Local | Variable | Good | Free | Multilingual voice cloning |

**Decision:** Kokoro-82M locally on 4090. ElevenLabs as quality fallback.

## Real-Time Voice Frameworks

| Framework | Architecture | Best For |
|-----------|-------------|----------|
| **Pipecat** (Daily.co) | Python pipeline, pluggable STT/LLM/TTS | CLI integration, maximum control |
| **LiveKit Agents** | WebRTC-first | Browser-based, production scale |
| **Vocode** | Low-level modular | Maximum control, most engineering |
| **OpenAI Realtime API** | Native speech-to-speech | OpenAI-locked, tool calling mid-conversation |
| **Gemini Live API** | Native audio-in/audio-out | Sub-second latency (320ms p50), no STT/TTS needed |

**Key finding:** Anthropic has NO public Realtime API as of March 2026. Voice Mode for Claude Code rolling out (~5% of users, March 3, 2026).

**Decision:** Use Gemini Live for voice (native, subscription), Claude for extraction/analysis. Pipecat as fallback if we need local STT/TTS pipeline.

## Conversation Memory Frameworks

| Framework | Type | Key Feature |
|-----------|------|-------------|
| **Mem0** | Managed/self-hosted | Graph-based memory, auto-extracts facts |
| **Letta** | Self-hosted | Self-editing memory, agents decide what to keep |
| **Zep** | Managed/self-hosted | Session-based with entity extraction |
| **Cognee** | Self-hosted | Knowledge graph construction |

**Decision:** Use existing PostgreSQL + AGE graph instead of adding Mem0. Unified node model means ideas, plans, and code share the same table and graph.

## Architecture Decisions

1. **Split-brain model roles:** Gemini Live for voice conversation, Claude for extraction/structured output, Codex for code review. All on subscription = no per-token cost for voice layer.
2. **Transport-agnostic pipeline:** Voice and text produce identical node structures. Only `input_mode` attribute differs. Everything downstream is shared.
3. **Context router over chat history:** Each model gets curated context from DB queries, not raw conversation replay. Intent classification determines which queries run.
4. **Unified node model:** Code, plans, conversations, ideas, findings all in same `nodes` table. AGE graph connects across domains.
5. **Local inference stack:** faster-whisper + Kokoro-82M + Silero VAD + nomic-embed-text all run on RTX 4090 with zero API cost.

## Sources

- Deepgram speech-to-text comparison 2026
- AssemblyAI real-time speech recognition 2026
- ElevenLabs alternatives (open-source TTS) 2026
- LiveKit vs Pipecat comparison (F22 Labs)
- OpenAI Realtime API vs Gemini Live 2025
- Anthropic Voice Mode for Claude Code (March 2026, Dataconomy)
- Mem0 AI memory for voice agents
- Agent memory comparison: Letta vs Mem0 vs Zep vs Cognee
- Whisper GPU benchmarks (Tom's Hardware)
- Pipecat GitHub, Kokoro-82M HuggingFace
