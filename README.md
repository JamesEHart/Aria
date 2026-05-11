# ARIA v2 — Local AI Agent
### Hermes-comparable · 100% Local · 100% Free · Zero Dependencies

---

## Feature comparison

| Feature | Hermes Agent | ARIA v2 |
|---|---|---|
| Local LLM (Ollama) | ✅ | ✅ |
| Dual memory (MEMORY.md + USER.md) | ✅ | ✅ |
| Memory capacity management | ✅ 2200+1375 chars | ✅ Same limits |
| Skills system (procedural memory) | ✅ | ✅ |
| Agent creates skills autonomously | ✅ | ✅ |
| Skill patch/edit/delete | ✅ | ✅ |
| SQLite session storage | ✅ FTS5 | ✅ FTS5 |
| Session search across history | ✅ | ✅ |
| Multi-step agentic tool loop | ✅ | ✅ |
| Code execution (Python/Bash) | ✅ | ✅ |
| Web search (DuckDuckGo) | ✅ | ✅ |
| Wikipedia research | ✅ | ✅ |
| File read/write | ✅ | ✅ |
| RAG (vector + FTS hybrid) | ✅ | ✅ |
| Self-evaluation & SOUL.md rewriting | ✅ | ✅ |
| Scheduled tasks (cron) | ✅ | ✅ |
| SOUL.md personality file | ✅ | ✅ |
| Live soul editor in UI | — | ✅ |
| Messaging platforms | ✅ 20+ | ❌ (browser only) |
| Voice mode | ✅ | ❌ |
| Sub-agents | ✅ | ❌ |
| Docker/SSH backends | ✅ | ❌ |
| Skills Hub (community) | ✅ | ❌ |

---

## Setup (3 steps)

### 1. Install Ollama
```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

### 2. Pull a model
```bash
ollama pull llama3.2        # Recommended (2GB, fast)
ollama pull mistral         # Better quality (4GB)
ollama pull qwen2.5:7b      # Excellent reasoning
```

### 3. Run ARIA
```bash
python3 server.py
```
Open **http://localhost:7842**

---

## Data location
Everything lives in `~/.aria/`:
```
~/.aria/
├── state.db              ← SQLite: sessions, messages (FTS5), knowledge, cron
├── SOUL.md               ← Evolving system personality (editable in UI)
├── evolution.json        ← Generation history
├── memories/
│   ├── MEMORY.md         ← Agent facts (2200 char cap, § delimited)
│   └── USER.md           ← User profile (1375 char cap)
└── skills/
    ├── my-skill/
    │   └── SKILL.md      ← Agent-created procedural memory
    └── ...
```

---

## How tools work in chat

ARIA uses a multi-step agentic loop. When you ask something complex, it will:
1. Call `knowledge_search` to check what it knows
2. Call `wikipedia` or `web_search` for new info  
3. Call `execute_code` to run Python/Bash if needed
4. Call `memory(add)` to save important facts
5. Call `skill_create` after complex multi-step tasks
6. Return the final answer

Tool calls are logged — click **"Tool log"** on any AI response to see exactly what it did.

---

## Slash commands in chat
```
/skills          — list all skills
/skill-name      — invoke a specific skill
```

---

## Environment variables
```bash
ARIA_PORT=8080 python3 server.py   # Change port
```
