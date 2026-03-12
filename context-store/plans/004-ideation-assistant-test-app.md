<![CDATA[# Plan 004 — Ideation Assistant Test App

<plan level="L2" task="Build test app: C# Minimal API + React/Vite frontend for ideation assistant">

  <context>
    CodeStoragePoc has a proven data layer: nodes, graph, context router (intent → assemble → prompt),
    agent navigation, advisory locks, embeddings. All verified via 13-step demo (`dotnet run`).

    What's missing: no user-facing app. The context router assembles prompts but never sends them to
    a model. No way to actually have a conversation, see threads, or park ideas.

    Existing infrastructure:
    - PostgreSQL 16 + AGE + pgvector on port 5433 (Docker)
    - NodeRepository, ContextAssembler, PromptBuilder, IntentClassifier — all working C# classes
    - RTX 4090 + 3090 with Ollama (nomic-embed-text for embeddings)
    - Claude API key (subscription) — official C# SDK: `Anthropic` NuGet v12.8.0
    - Port 5175 free for Vite, 5102 free for C# API

    MEMORY.md notes:
    - Transport-agnostic design means voice can plug in later without changing the pipeline
    - Context routing > chat history — models get curated context, not raw replay
    - Plans as nodes — this plan itself will be stored in DB
  </context>

  <thinking>
    The goal is a working conversation loop: type → classify → assemble context → call Claude →
    store response as turn → extract ideas → display. The UI needs to show the conversation AND
    the extracted knowledge (threads, ideas, stats).

    **API design:** Minimal API in a separate C# project that references CodeStoragePoc. This keeps
    the demo app (Program.cs) and the API separate. The API project can use all existing classes
    directly via project reference.

    **Model integration:** Use the official Anthropic C# SDK (`Anthropic` NuGet). The PromptBuilder
    already produces XML-structured prompts — we just need to send them to Claude and parse the
    response. For the first version, Claude does double duty: conversation + extraction. Later we
    split (Gemini for voice, Claude for extraction).

    **Extraction pipeline:** After Claude responds, we need to extract ideas/questions/decisions
    from the response. For v1, this can be a second Claude call with a structured extraction prompt.
    The extracted items become nodes in the DB.

    **Frontend:** React + Vite + Tailwind. Three-panel layout:
    - Left: threads sidebar (open ideas, parked, questions)
    - Center: chat panel (conversation with speaker badges)
    - Right: stats/context (what the model saw, intent, stats)

    **Conversation persistence:** Each browser session creates or resumes a conversation node.
    Turns are stored as nodes. The conversation is rebuilt from DB on page load.

    **SSE for streaming:** Claude API supports streaming. Use Server-Sent Events from the C# API
    to stream responses to the frontend.
  </thinking>

  <approach>
    <step n="1">Create Api/ project — C# Minimal API with project reference to CodeStoragePoc.
    Add Anthropic NuGet. Configure CORS for Vite dev server. Wire up NodeRepository, ContextAssembler,
    PromptBuilder, IntentClassifier as singletons.</step>

    <step n="2">Build core chat endpoint — POST /api/chat accepts { message, conversationId? }.
    Creates conversation if needed, stores user turn, classifies intent, assembles context, calls
    Claude via Anthropic SDK, stores model turn, returns response. SSE streaming.</step>

    <step n="3">Build extraction pipeline — after model response, make a second Claude call with
    extraction prompt. Parse structured output (ideas, questions, decisions). Insert as nodes under
    the conversation's topic. Return extracted items alongside the response.</step>

    <step n="4">Build read endpoints — GET /api/conversations (list), GET /api/conversation/:id
    (full thread), GET /api/threads/:conversationId (open/parked ideas), GET /api/stats/:conversationId
    (counts).</step>

    <step n="5">Build command endpoints — POST /api/command/park { ideaId },
    POST /api/command/resume { ideaId }, POST /api/command/review { conversationId }.
    Direct DB mutations, no model call needed.</step>

    <step n="6">Scaffold React/Vite/Tailwind frontend — npm create vite, install deps, configure
    proxy to API on 5102.</step>

    <step n="7">Build ChatPanel component — message list with speaker badges (user/claude/gemini),
    input box, send button. Streaming display via SSE/EventSource.</step>

    <step n="8">Build ThreadsSidebar component — lists ideas grouped by status (explored, mentioned,
    parked). Click to highlight in conversation. Park/resume buttons.</step>

    <step n="9">Build StatsBar component — shows intent classification, total ideas, open questions,
    parked count. Updates after each message.</step>

    <step n="10">Wire it all together — conversation persistence (localStorage for conversationId),
    auto-scroll, keyboard shortcuts, error handling. Test full loop.</step>
  </approach>

  <outputs>
    <file path="Api/Api.csproj" action="create">Minimal API project, references CodeStoragePoc</file>
    <file path="Api/Program.cs" action="create">Endpoints, DI setup, CORS, SSE streaming</file>
    <file path="Api/Services/ChatService.cs" action="create">Orchestrates: classify → assemble → call Claude → store → extract</file>
    <file path="Api/Services/ExtractionService.cs" action="create">Claude-based idea extraction from responses</file>
    <file path="ui/package.json" action="create">React + Vite + Tailwind deps</file>
    <file path="ui/vite.config.ts" action="create">Dev server on 5175, proxy /api to 5102</file>
    <file path="ui/src/App.tsx" action="create">Three-panel layout</file>
    <file path="ui/src/components/ChatPanel.tsx" action="create">Conversation view with streaming</file>
    <file path="ui/src/components/ThreadsSidebar.tsx" action="create">Ideas by status, park/resume</file>
    <file path="ui/src/components/StatsBar.tsx" action="create">Intent, counts, context debug</file>
    <file path="ui/src/api.ts" action="create">Fetch wrappers, SSE helper</file>
    <file path="ui/tailwind.config.js" action="create">Tailwind config</file>
    <file path="port-registry.md" action="modify">Add ports 5102, 5175</file>
    <file path="CLAUDE.md" action="modify">Add Api/ and ui/ to project structure</file>
  </outputs>

  <testing>
    <verify>dotnet build Api/ — 0 errors</verify>
    <verify>npm run dev in ui/ — Vite starts on 5175</verify>
    <verify>POST /api/chat with message — returns Claude response + extracted ideas</verify>
    <verify>GET /api/threads/:id — returns open/parked ideas from DB</verify>
    <verify>Full loop: type message → see response stream → see extracted idea appear in sidebar</verify>
    <manual>Park an idea via sidebar button → verify status changes in DB</manual>
    <manual>Start new conversation → verify new conversation node created</manual>
    <manual>Refresh page → verify conversation reloads from DB</manual>
  </testing>

  <questions>
    <question n="1">
      <ask>Should the extraction happen synchronously (user waits) or async (ideas appear after)?</ask>
      <proposed>Synchronous for v1 — extraction adds ~1-2s but the user sees ideas immediately.
      Async optimization later if latency matters.</proposed>
    </question>
    <question n="2">
      <ask>Which Claude model for conversation vs extraction?</ask>
      <proposed>claude-sonnet-4-6 for both. Fast enough for conversation, good enough for extraction.
      Upgrade to opus for complex ideation sessions later.</proposed>
    </question>
    <question n="3">
      <ask>Should we embed new ideas immediately or batch?</ask>
      <proposed>Immediately via Ollama nomic-embed-text. One embedding per idea is fast (~50ms).
      Enables semantic search right away.</proposed>
    </question>
  </questions>

  <risks>
    <assumption>Claude API key is set as ANTHROPIC_API_KEY environment variable</assumption>
    <assumption>PostgreSQL container is running on port 5433</assumption>
    <blast_radius>New project — no impact on existing CodeStoragePoc demo</blast_radius>
    <rollback>Delete Api/ and ui/ directories. No schema changes.</rollback>
  </risks>

</plan>
]]>