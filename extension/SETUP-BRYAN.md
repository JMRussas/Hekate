# Hekate Fleet Control — Setup Guide

## What This Is

A VS Code sidebar that lets you chat with multiple AI providers (Gemini, Claude, Codex) directly from the editor. No backend server needed — the extension calls the CLIs on your machine.

## Setup (2 minutes)

### Step 1: Install a CLI

You need at least one AI CLI installed. Pick whichever you have a subscription for:

| Provider | Install | Auth |
|----------|---------|------|
| **Gemini** | `npm install -g @anthropic-ai/gemini-cli` | Google AI Studio login |
| **Claude** | `npm install -g @anthropic-ai/claude-code` | Anthropic Max subscription |
| **Codex** | `npm install -g @openai/codex` | OpenAI subscription |

Verify it works: `gemini -p "hello"` (or `claude -p "hello"`, etc.)

### Step 2: Install the Extension

```bash
code --install-extension hekate-fleet-0.2.0.vsix --force
```

Then **reload VS Code** (Ctrl+Shift+P → "Reload Window").

### Step 3: Use It

1. Click the **robot icon** in the left activity bar
2. The **Chat** panel is at the bottom of the sidebar
3. Pick your provider from the dropdown
4. Type a message and hit Enter

That's it. No server, no config, no API keys to paste.

## Chat Features

- **Provider dropdown** — switch between Gemini, Claude, Codex (and Ollama if local)
- **Stop button** — cancel a response mid-generation
- **Markdown** — code blocks, bold, italic render in responses
- **Conversation history** — maintains context across messages (last 20)
- **Slash commands** — type `/help` for fleet management commands

## Troubleshooting

**"CLI was not found"**
- Install the CLI (see table above) and reload VS Code
- Make sure it's on your PATH: open a terminal and run `gemini --version`

**Slow responses**
- First call may take a few seconds for the CLI to authenticate
- Claude and Codex can take 10-30s depending on prompt complexity

**Fleet tree shows "Not connected"**
- This is normal — the fleet tree is for orchestration projects (optional)
- Chat works independently without any backend
