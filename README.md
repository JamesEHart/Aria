# ARIA - Local AI Agent

A personal AI assistant that runs entirely on your machine via Ollama. Persistent memory, autonomous skill creation, self-evaluation, agentic tool use - no API keys, no cloud, no cost.

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) running locally

---

## Setup

### 1. Install Ollama
```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows - download installer from https://ollama.com
```

### 2. Pull a model
```bash
ollama pull llama3.2        # 2GB, fast - good starting point
ollama pull mistral         # 4GB, better quality
ollama pull qwen2.5:7b      # excellent reasoning
```

### 3. Run ARIA
```bash
python3 server.py
```
Open **http://localhost:7842**

---

## UI Overview

Three-panel layout:

**Left sidebar** - 7 tabs (Config / Memory / Skills / Code / KB / Sessions / Cron)
- **Config** - Ollama URL, model selector with live readiness check, quick actions, stats
- **Memory** - view and edit agent memory (facts) and user profile
- **Skills** - browse, create, and edit procedural skills
- **Code** - search Stack Overflow, MDN, DevDocs, GitHub, PyPI, npm
- **KB** - full-text + vector knowledge base, paste notes and docs
- **Sessions** - browse and search past conversations
- **Cron** - schedule recurring tasks

**Center** - chat interface with tool log per message

**Right sidebar** - SOUL.md viewer (click to edit), evolution log

---

## How the agentic loop works

When you send a message, ARIA runs a multi-step loop before replying:

1. Searches memory and knowledge base for relevant context
2. Calls `web_search` or `wikipedia` if it needs new information
3. Runs `execute_code` for Python/Bash when needed
4. Saves important facts with `memory(add)`
5. Creates a skill with `skill_create` after solving complex tasks
6. Returns the final answer

Tool calls are visible - click **"Tool log"** on any AI response to inspect every step.

---

## Slash commands

```
/skills          - list all available skills
/skill-name      - invoke a specific skill by name
```

---

## Data

Everything lives in `~/.aria/` - nothing leaves your machine:

```
~/.aria/
├── state.db              ← SQLite: sessions, messages (FTS5), knowledge, cron jobs
├── SOUL.md               ← Evolving system personality (editable in the UI)
├── evolution.json        ← Generation history and self-evaluation scores
├── memories/
│   ├── MEMORY.md         ← Agent facts (2200 char cap, § delimited)
│   └── USER.md           ← User profile and preferences (1375 char cap)
└── skills/
    └── skill-name/
        └── SKILL.md      ← Agent-created procedural memory
```

---

## Environment variables

```bash
ARIA_PORT=8080 python3 server.py
```

---

## Feature comparison

| Feature | Hermes Agent | ARIA |
|---|---|---|
| Local LLM (Ollama) | ✅ | ✅ |
| Live model readiness check | - | ✅ `/api/ps` + generate ping |
| Dual memory (facts + user profile) | ✅ | ✅ |
| Memory capacity management | ✅ 2200+1375 chars | ✅ same limits |
| Skills system (procedural memory) | ✅ | ✅ |
| Agent creates skills autonomously | ✅ | ✅ |
| Skill create/edit/delete in UI | ✅ | ✅ |
| SQLite session storage (FTS5) | ✅ | ✅ |
| Session search across history | ✅ | ✅ |
| Multi-step agentic tool loop | ✅ | ✅ |
| Code execution (Python/Bash) | ✅ | ✅ |
| Web search (DuckDuckGo) | ✅ | ✅ |
| Wikipedia research | ✅ | ✅ |
| File read/write | ✅ | ✅ |
| RAG (vector + FTS hybrid) | ✅ | ✅ |
| Dev resource search (SO, MDN, etc.) | - | ✅ |
| Self-evaluation & SOUL.md rewriting | ✅ | ✅ |
| Scheduled tasks (cron) | ✅ | ✅ |
| SOUL.md personality file | ✅ | ✅ |
| Live soul editor in UI | - | ✅ |
| Messaging platforms | ✅ 20+ | ❌ browser only |
| Voice mode | ✅ | ❌ |
| Sub-agents | ✅ | ❌ |
| Docker/SSH backends | ✅ | ❌ |
| Skills Hub (community) | ✅ | ❌ |
