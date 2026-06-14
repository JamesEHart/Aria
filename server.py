#!/usr/bin/env python3
"""
ARIA v2 — Adaptive Research Intelligence Assistant
Hermes-comparable local agent: Ollama + SQLite/FTS5 + Skills + Dual Memory + Tools + Cron
Zero external dependencies beyond Python stdlib.
"""

import json, os, re, time, hashlib, math, datetime, sqlite3, subprocess, threading
import socketserver, shutil, glob, sys
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError
from urllib.parse import quote, urlparse, parse_qs

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE      = Path(__file__).parent
STATIC    = BASE / "static"
DATA      = Path.home() / ".aria"
MEMORY_D  = DATA / "memories"
SKILLS_D  = DATA / "skills"
DB_PATH   = DATA / "state.db"
SOUL_FILE = DATA / "SOUL.md"
LOG_FILE  = DATA / "evolution.json"

for d in [DATA, MEMORY_D, SKILLS_D]: d.mkdir(parents=True, exist_ok=True)

DEFAULT_SOUL = """You are ARIA, a self-improving local AI agent.
You are direct, honest, curious, and admit uncertainty.
You proactively save important user preferences and facts to memory.
You create skills after solving complex tasks so you never repeat yourself.
When [RESEARCH CONTEXT] is injected, prioritise it.
Keep responses concise unless asked for detail."""

DEFAULT_OLLAMA = "http://localhost:11434"
MEMORY_LIMIT   = 2200   # chars, like Hermes
USER_LIMIT     = 1375   # chars

# ── Database ───────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY, session_id INTEGER, role TEXT,
        content TEXT, ts TEXT, metadata TEXT)""")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
        USING fts5(content, content='messages', content_rowid='id')""")
    db.execute("""CREATE TABLE IF NOT EXISTS cron_jobs(
        id INTEGER PRIMARY KEY, name TEXT UNIQUE, schedule TEXT,
        prompt TEXT, last_run TEXT, enabled INTEGER DEFAULT 1)""")
    db.execute("""CREATE TABLE IF NOT EXISTS knowledge(
        id TEXT PRIMARY KEY, topic TEXT, content TEXT, source TEXT,
        url TEXT, created_at TEXT, embedding_json TEXT)""")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
        USING fts5(topic, content, content='knowledge', content_rowid='rowid')""")
    db.commit()
    return db

_db = None
_db_lock = threading.Lock()
def db():
    global _db
    with _db_lock:
        if _db is None: _db = get_db()
        return _db

# ── Memory files (MEMORY.md + USER.md like Hermes) ────────────────────────────
MEMORY_FILE = MEMORY_D / "MEMORY.md"
USER_FILE   = MEMORY_D / "USER.md"
EVO_FILE    = DATA / "evolution.json"

def _read_mem(path: Path) -> list[str]:
    if not path.exists(): return []
    raw = path.read_text().strip()
    if not raw: return []
    return [e.strip() for e in raw.split("§") if e.strip()]

def _write_mem(path: Path, entries: list[str]):
    path.write_text(" § ".join(entries) if entries else "")

def _mem_chars(entries: list[str]) -> int:
    return sum(len(e) for e in entries) + max(0, (len(entries)-1)*3)

def memory_load() -> dict:
    mem = _read_mem(MEMORY_FILE)
    usr = _read_mem(USER_FILE)
    return {
        "memory": mem, "user": usr,
        "memory_chars": _mem_chars(mem), "memory_limit": MEMORY_LIMIT,
        "user_chars": _mem_chars(usr),   "user_limit": USER_LIMIT,
    }

def memory_action(target: str, action: str, content: str = "", old_text: str = "") -> dict:
    path  = MEMORY_FILE if target == "memory" else USER_FILE
    limit = MEMORY_LIMIT if target == "memory" else USER_LIMIT
    entries = _read_mem(path)

    if action == "add":
        # duplicate check
        if any(content.strip() in e for e in entries):
            return {"ok": True, "note": "duplicate skipped"}
        new_chars = _mem_chars(entries) + len(content) + 3
        if new_chars > limit:
            return {"ok": False, "error": f"{target} at {_mem_chars(entries)}/{limit} chars. "
                    f"Adding this ({len(content)} chars) would exceed limit. "
                    f"Replace or remove entries first.", "entries": entries}
        entries.append(content.strip())
        _write_mem(path, entries)
        return {"ok": True, "chars": _mem_chars(entries), "limit": limit}

    elif action == "replace":
        hits = [i for i,e in enumerate(entries) if old_text in e]
        if len(hits) == 0: return {"ok": False, "error": f"'{old_text}' not found"}
        if len(hits) > 1:  return {"ok": False, "error": f"'{old_text}' matches {len(hits)} entries — be more specific"}
        entries[hits[0]] = content.strip()
        _write_mem(path, entries)
        return {"ok": True}

    elif action == "remove":
        hits = [i for i,e in enumerate(entries) if old_text in e]
        if not hits: return {"ok": False, "error": f"'{old_text}' not found"}
        if len(hits) > 1: return {"ok": False, "error": f"'{old_text}' matches multiple entries"}
        entries.pop(hits[0])
        _write_mem(path, entries)
        return {"ok": True}

    elif action == "list":
        return {"ok": True, "entries": entries, "chars": _mem_chars(entries), "limit": limit}

    return {"ok": False, "error": "unknown action"}

def memory_block() -> str:
    """Format memory for injection into system prompt."""
    m = memory_load()
    lines = []
    if m["memory"]:
        pct = int(m["memory_chars"]/m["memory_limit"]*100)
        lines.append(f"══ MEMORY [{pct}% — {m['memory_chars']}/{m['memory_limit']} chars] ══")
        lines.extend(m["memory"])
    if m["user"]:
        pct = int(m["user_chars"]/m["user_limit"]*100)
        lines.append(f"══ USER PROFILE [{pct}% — {m['user_chars']}/{m['user_limit']} chars] ══")
        lines.extend(m["user"])
    return "\n".join(lines)

# ── Skills system ──────────────────────────────────────────────────────────────
def skills_list() -> list[dict]:
    skills = []
    for skill_dir in sorted(SKILLS_D.iterdir()):
        sk_file = skill_dir / "SKILL.md"
        if skill_dir.is_dir() and sk_file.exists():
            raw = sk_file.read_text()
            name = skill_dir.name
            desc = ""
            # Parse frontmatter
            fm_match = re.match(r'^---\n(.*?)\n---', raw, re.DOTALL)
            if fm_match:
                fm = fm_match.group(1)
                nm = re.search(r'^name:\s*(.+)$', fm, re.M)
                ds = re.search(r'^description:\s*(.+)$', fm, re.M)
                if nm: name = nm.group(1).strip()
                if ds: desc = ds.group(1).strip()
            else:
                # First heading
                h = re.search(r'^# (.+)$', raw, re.M)
                if h: desc = h.group(1).strip()
            skills.append({"name": name, "dir": skill_dir.name,
                           "description": desc, "path": str(sk_file)})
    return skills

def skill_view(name: str) -> str | None:
    for skill_dir in SKILLS_D.iterdir():
        sk_file = skill_dir / "SKILL.md"
        if skill_dir.is_dir() and sk_file.exists():
            if skill_dir.name == name or skill_dir.name.replace("-","_") == name.replace("-","_"):
                return sk_file.read_text()
    return None

def skill_create(name: str, content: str) -> dict:
    slug = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
    skill_dir = SKILLS_D / slug
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    return {"ok": True, "path": str(skill_dir / "SKILL.md"), "name": slug}

def skill_patch(name: str, old_str: str, new_str: str) -> dict:
    content = skill_view(name)
    if content is None: return {"ok": False, "error": "skill not found"}
    if old_str not in content: return {"ok": False, "error": "old_str not found"}
    new_content = content.replace(old_str, new_str, 1)
    slug = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
    (SKILLS_D / slug / "SKILL.md").write_text(new_content)
    return {"ok": True}

def skill_delete(name: str) -> dict:
    slug = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
    skill_dir = SKILLS_D / slug
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        return {"ok": True}
    return {"ok": False, "error": "not found"}

def skill_index_for_prompt() -> str:
    skills = skills_list()
    if not skills: return ""
    lines = ["══ SKILLS ══"]
    for s in skills:
        lines.append(f"/{s['name']} — {s['description']}")
    return "\n".join(lines)

# ── Soul / System prompt ───────────────────────────────────────────────────────
def load_soul() -> str:
    if SOUL_FILE.exists(): return SOUL_FILE.read_text().strip()
    return DEFAULT_SOUL

def save_soul(text: str): SOUL_FILE.write_text(text)

def build_system_prompt() -> str:
    parts = [load_soul(), ""]
    mb = memory_block()
    if mb: parts.append(mb + "\n")
    si = skill_index_for_prompt()
    if si: parts.append(si + "\n")
    return "\n".join(parts)

# ── Simple keyword embedding ───────────────────────────────────────────────────
STOP = {'the','a','an','and','or','but','in','on','at','to','for','of','with',
        'by','from','is','was','are','were','be','been','has','have','had',
        'do','does','did','will','would','could','should','may','might',
        'i','you','he','she','it','we','they','this','that','these','those',
        'not','no','so','if','as','into','than','then','when','what','which',
        'who','how','its','our','your','their','my','his','her'}

def embed(text: str) -> dict:
    words = re.sub(r'[^a-z0-9\s]','',text.lower()).split()
    freq = {}
    for w in words:
        if len(w)>2 and w not in STOP: freq[w]=freq.get(w,0)+1
    return freq

def cosine(a: dict, b: dict) -> float:
    if not a or not b: return 0.0
    keys = set(a)|set(b)
    dot = sum(a.get(k,0)*b.get(k,0) for k in keys)
    ma = math.sqrt(sum(v*v for v in a.values()))
    mb2 = math.sqrt(sum(v*v for v in b.values()))
    return dot/(ma*mb2) if ma and mb2 else 0.0

def rag_retrieve(query: str, top_k: int = 4) -> list:
    q = embed(query)
    rows = db().execute("SELECT id,topic,content,source,url,embedding_json FROM knowledge").fetchall()
    scored = []
    for r in rows:
        emb = json.loads(r["embedding_json"] or "{}")
        scored.append((cosine(q, emb), dict(r)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for s,r in scored[:top_k] if s > 0.04]

# ── Knowledge / research ───────────────────────────────────────────────────────
def knowledge_add(topic: str, content: str, source: str = "manual", url: str = "") -> dict:
    kid = hashlib.md5((topic+content[:100]).encode()).hexdigest()
    emb = json.dumps(embed(content+" "+topic))
    now = datetime.datetime.now().isoformat()
    try:
        db().execute(
            "INSERT OR REPLACE INTO knowledge(id,topic,content,source,url,created_at,embedding_json) VALUES(?,?,?,?,?,?,?)",
            (kid, topic, content, source, url, now, emb))
        db().execute("INSERT OR REPLACE INTO messages_fts(rowid,content) SELECT rowid,content FROM messages WHERE id=last_insert_rowid()")
        db().commit()
        return {"ok": True, "id": kid}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def knowledge_fts_search(query: str, limit: int = 10) -> list:
    try:
        rows = db().execute(
            "SELECT k.* FROM knowledge k JOIN knowledge_fts f ON k.rowid=f.rowid WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit)).fetchall()
        return [dict(r) for r in rows]
    except:
        # fallback to LIKE
        rows = db().execute(
            "SELECT * FROM knowledge WHERE topic LIKE ? OR content LIKE ? LIMIT ?",
            (f"%{query}%", f"%{query}%", limit)).fetchall()
        return [dict(r) for r in rows]

def wikipedia_fetch(topic: str) -> dict | None:
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(topic)}"
        req = Request(url, headers={"User-Agent":"ARIA/2.0"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        extract = data.get("extract","")
        if not extract: return None
        content = extract[:1200]
        result = knowledge_add(
            data.get("title", topic), content,
            f"Wikipedia: {data.get('title',topic)}",
            data.get("content_urls",{}).get("desktop",{}).get("page",""))
        return {"topic": data.get("title",topic), "content": content,
                "source": f"Wikipedia: {data.get('title',topic)}", **result}
    except: return None

def ddg_search(query: str) -> list[dict]:
    """DuckDuckGo instant answers API — free, no key."""
    try:
        url = f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_html=1&skip_disambig=1"
        req = Request(url, headers={"User-Agent":"ARIA/2.0"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        results = []
        if data.get("AbstractText"):
            results.append({"title": data.get("Heading","Result"),
                            "content": data["AbstractText"],
                            "url": data.get("AbstractURL","")})
        for rel in data.get("RelatedTopics",[])[:3]:
            if isinstance(rel,dict) and rel.get("Text"):
                results.append({"title": rel.get("FirstURL","").split("/")[-1].replace("_"," "),
                                "content": rel["Text"],
                                "url": rel.get("FirstURL","")})
        return results[:4]
    except: return []

# ── Coding research sources ────────────────────────────────────────────────────

def stackoverflow_search(query: str, limit: int = 5) -> list[dict]:
    """Stack Overflow API v2.3 — free, no key, 300 req/day unauth."""
    try:
        params = urlencode_simple({
            "order": "desc", "sort": "relevance", "intitle": query,
            "site": "stackoverflow", "pagesize": str(limit),
            "filter": "withbody"   # includes body HTML
        })
        url = f"https://api.stackexchange.com/2.3/search/advanced?{params}"
        req = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "ARIA/2.0"})
        with urlopen(req, timeout=10) as r: data = json.loads(r.read())
        results = []
        for item in data.get("items", [])[:limit]:
            title   = item.get("title", "")
            link    = item.get("link", "")
            tags    = ", ".join(item.get("tags", [])[:5])
            score   = item.get("score", 0)
            answers = item.get("answer_count", 0)
            accepted = item.get("is_answered", False)
            snippet = strip_html(item.get("body", ""))[:600]
            content = (f"Tags: {tags} | Score: {score} | Answers: {answers}"
                       f" | Accepted: {accepted}\n\n{snippet}")
            results.append({"title": title, "content": content,
                            "url": link, "source": "stackoverflow",
                            "score": score, "accepted": accepted})
        return results
    except Exception as e:
        return [{"error": str(e)}]

def stackoverflow_answers(question_id: str) -> list[dict]:
    """Fetch top answers for a specific SO question."""
    try:
        params = urlencode_simple({
            "order": "desc", "sort": "votes", "site": "stackoverflow",
            "filter": "withbody", "pagesize": "3"
        })
        url = f"https://api.stackexchange.com/2.3/questions/{question_id}/answers?{params}"
        req = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "ARIA/2.0"})
        with urlopen(req, timeout=10) as r: data = json.loads(r.read())
        results = []
        for ans in data.get("items", [])[:3]:
            body = strip_html(ans.get("body", ""))[:1000]
            results.append({
                "answer_id": ans.get("answer_id"),
                "score": ans.get("score", 0),
                "accepted": ans.get("is_accepted", False),
                "content": body,
                "url": f"https://stackoverflow.com/a/{ans.get('answer_id')}"
            })
        return results
    except: return []

def mdn_search(query: str) -> list[dict]:
    """MDN Web Docs search API — free, no key."""
    try:
        url = f"https://developer.mozilla.org/api/v1/search?q={quote(query)}&locale=en-US&size=4"
        req = Request(url, headers={"User-Agent": "ARIA/2.0"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        results = []
        for doc in data.get("documents", [])[:4]:
            results.append({
                "title": doc.get("title", ""),
                "content": doc.get("summary", "")[:600],
                "url": "https://developer.mozilla.org" + doc.get("mdn_url", ""),
                "source": "mdn"
            })
        return results
    except: return []

def pypi_search(package: str) -> dict:
    """PyPI JSON API — free, no key."""
    try:
        url = f"https://pypi.org/pypi/{quote(package)}/json"
        req = Request(url, headers={"User-Agent": "ARIA/2.0"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        info = data.get("info", {})
        return {
            "name": info.get("name", package),
            "version": info.get("version", ""),
            "summary": info.get("summary", ""),
            "home_page": info.get("home_page", ""),
            "docs_url": info.get("docs_url", ""),
            "project_url": info.get("project_url", ""),
            "requires_python": info.get("requires_python", ""),
            "license": info.get("license", ""),
            "content": f"{info.get('summary','')}\nVersion: {info.get('version','')}\nLicense: {info.get('license','')}\nPython: {info.get('requires_python','')}",
            "source": "pypi",
            "url": f"https://pypi.org/project/{package}/"
        }
    except: return {"error": f"Package '{package}' not found on PyPI"}

def npm_search(package: str) -> dict:
    """npm registry API — free, no key."""
    try:
        url = f"https://registry.npmjs.org/{quote(package)}/latest"
        req = Request(url, headers={"User-Agent": "ARIA/2.0"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        deps = list(data.get("dependencies", {}).keys())[:8]
        content = (f"{data.get('description','')}\n"
                   f"Version: {data.get('version','')}\n"
                   f"License: {data.get('license','')}\n"
                   f"Dependencies: {', '.join(deps) or 'none'}")
        return {
            "name": data.get("name", package),
            "version": data.get("version", ""),
            "description": data.get("description", ""),
            "license": data.get("license", ""),
            "content": content,
            "source": "npm",
            "url": f"https://www.npmjs.com/package/{package}"
        }
    except: return {"error": f"Package '{package}' not found on npm"}

def github_search_repos(query: str, limit: int = 4) -> list[dict]:
    """GitHub search API — free, 10 req/min unauth."""
    try:
        url = f"https://api.github.com/search/repositories?q={quote(query)}&sort=stars&order=desc&per_page={limit}"
        req = Request(url, headers={"User-Agent": "ARIA/2.0",
                                    "Accept": "application/vnd.github.v3+json"})
        with urlopen(req, timeout=10) as r: data = json.loads(r.read())
        results = []
        for repo in data.get("items", [])[:limit]:
            content = (f"{repo.get('description','No description')}\n"
                       f"Language: {repo.get('language','')}\n"
                       f"Stars: {repo.get('stargazers_count',0):,}\n"
                       f"Topics: {', '.join(repo.get('topics',[])[:5])}")
            results.append({
                "title": repo.get("full_name", ""),
                "content": content,
                "url": repo.get("html_url", ""),
                "stars": repo.get("stargazers_count", 0),
                "language": repo.get("language", ""),
                "source": "github"
            })
        return results
    except: return []

def github_readme(owner_repo: str) -> dict:
    """Fetch README for a GitHub repo."""
    try:
        url = f"https://api.github.com/repos/{owner_repo}/readme"
        req = Request(url, headers={"User-Agent": "ARIA/2.0",
                                    "Accept": "application/vnd.github.raw"})
        with urlopen(req, timeout=10) as r: content = r.read().decode(errors="replace")
        # Strip badges and strip long lines
        lines = [l for l in content.split("\n") if not l.startswith("[![")]
        return {"content": "\n".join(lines)[:2000], "source": "github_readme",
                "url": f"https://github.com/{owner_repo}"}
    except Exception as e: return {"error": str(e)}

def devdocs_search(query: str, docsets: str = "python,javascript,css,html,bash") -> list[dict]:
    """DevDocs.io — scrape search endpoint (no official API but publicly accessible)."""
    try:
        url = f"https://devdocs.io/search.json?q={quote(query)}"
        req = Request(url, headers={"User-Agent": "ARIA/2.0"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        results = []
        for entry in data.get("results", [])[:6]:
            results.append({
                "title": f"{entry.get('doc',{}).get('name','')} — {entry.get('name','')}",
                "content": entry.get("excerpt", entry.get("name", ""))[:400],
                "url": f"https://devdocs.io/{entry.get('doc',{}).get('slug','')}/{entry.get('path','')}",
                "source": "devdocs",
                "doc": entry.get("doc", {}).get("name", "")
            })
        return results
    except: return []

def caniuse_search(feature: str) -> dict:
    """Can I Use — CSS/HTML/JS browser compatibility data."""
    try:
        # caniuse data is available as a public JSON on GitHub
        url = f"https://raw.githubusercontent.com/Fyrd/caniuse/main/features-json/{quote(feature.lower().replace(' ','-'))}.json"
        req = Request(url, headers={"User-Agent": "ARIA/2.0"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        title = data.get("title", feature)
        desc  = data.get("description", "")
        spec  = data.get("spec", "")
        # Summarise browser support
        stats = data.get("stats", {})
        support = {}
        for browser, versions in stats.items():
            latest = sorted(versions.items(), key=lambda x: x[0])[-1]
            support[browser] = latest[1]  # y/n/a/p
        return {
            "title": title, "description": desc, "spec": spec,
            "support": support, "source": "caniuse",
            "content": f"{desc}\nBrowser support: {json.dumps(support)}\nSpec: {spec}",
            "url": f"https://caniuse.com/{feature}"
        }
    except: return {"error": f"Feature '{feature}' not found on Can I Use"}

def tldr_pages(command: str) -> dict:
    """tldr-pages — simplified man pages. Free GitHub raw."""
    for platform in ["linux", "common", "osx", "windows"]:
        try:
            url = f"https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/{platform}/{quote(command.lower())}.md"
            req = Request(url, headers={"User-Agent": "ARIA/2.0"})
            with urlopen(req, timeout=6) as r: content = r.read().decode(errors="replace")
            return {"command": command, "platform": platform,
                    "content": content[:1500], "source": "tldr",
                    "url": f"https://tldr.inbrowser.app/pages/{platform}/{command}"}
        except: continue
    return {"error": f"No tldr page found for '{command}'"}

def python_docs_search(query: str) -> list[dict]:
    """Search Python official docs via docs.python.org search."""
    try:
        url = f"https://docs.python.org/3/search.html?q={quote(query)}&check_keywords=yes&area=default"
        # Use the JSON search index instead
        index_url = "https://docs.python.org/3/objects.inv"
        # Actually use the simpler search API
        url2 = f"https://docs.python.org/3/_/search/?q={quote(query)}&check_keywords=yes&area=default"
        req = Request(url2, headers={"User-Agent": "ARIA/2.0",
                                     "Accept": "application/json"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        results = []
        for item in data.get("hits", {}).get("hits", [])[:4]:
            src = item.get("_source", {})
            results.append({
                "title": src.get("title",""),
                "content": src.get("content","")[:400],
                "url": "https://docs.python.org/3/" + src.get("url",""),
                "source": "python_docs"
            })
        return results
    except:
        # Fallback: search devdocs which indexes python docs
        return devdocs_search(f"python {query}")

def rust_docs_search(query: str) -> list[dict]:
    """docs.rs search — Rust crate documentation."""
    try:
        url = f"https://docs.rs/search?q={quote(query)}&limit=4"
        req = Request(url, headers={"User-Agent": "ARIA/2.0",
                                    "Accept": "application/json"})
        with urlopen(req, timeout=8) as r: data = json.loads(r.read())
        results = []
        for crate in data.get("results", [])[:4]:
            results.append({
                "title": crate.get("name",""),
                "content": f"{crate.get('description','')}\nVersion: {crate.get('version','')}",
                "url": f"https://docs.rs/{crate.get('name','')}",
                "source": "docs_rs"
            })
        return results
    except: return []

def code_search_unified(query: str, sources: list = None) -> dict:
    """Run query across multiple coding sources and merge results."""
    if sources is None:
        sources = ["stackoverflow", "mdn", "devdocs", "github"]
    results = {}
    threads = []
    lock = threading.Lock()

    def fetch(src):
        try:
            if src == "stackoverflow":
                r = stackoverflow_search(query, 3)
            elif src == "mdn":
                r = mdn_search(query)
            elif src == "devdocs":
                r = devdocs_search(query)
            elif src == "github":
                r = github_search_repos(query, 3)
            elif src == "pypi":
                r = [pypi_search(query)]
            elif src == "npm":
                r = [npm_search(query)]
            elif src == "tldr":
                r = [tldr_pages(query)]
            else:
                r = []
            with lock:
                results[src] = r
                # Persist to knowledge DB
                for item in (r if isinstance(r, list) else [r]):
                    if item and not item.get("error") and item.get("content"):
                        knowledge_add(
                            item.get("title", query),
                            item.get("content","")[:800],
                            item.get("source", src),
                            item.get("url","")
                        )
        except Exception as e:
            with lock: results[src] = [{"error": str(e)}]

    for src in sources:
        t = threading.Thread(target=fetch, args=(src,))
        t.start(); threads.append(t)
    for t in threads: t.join(timeout=12)

    return results

# ── HTML stripping util ────────────────────────────────────────────────────────
def strip_html(html: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&#39;', "'", text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def urlencode_simple(params: dict) -> str:
    return "&".join(f"{k}={quote(str(v))}" for k, v in params.items())

# ── Ollama ─────────────────────────────────────────────────────────────────────
def ollama_chat(messages: list, system: str, model: str,
                ollama_url: str = DEFAULT_OLLAMA, max_tokens: int = 1000) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [{"role":"system","content":system}] + messages,
        "stream": False,
        "options": {"temperature":0.7,"num_predict":max_tokens}
    }).encode()
    req = Request(f"{ollama_url}/api/chat", data=payload,
                  headers={"Content-Type":"application/json"}, method="POST")
    with urlopen(req, timeout=180) as r:
        resp = json.loads(r.read())
    return resp.get("message",{}).get("content","").strip()

def ollama_models(url: str) -> list[str]:
    try:
        req = Request(f"{url}/api/tags", headers={"User-Agent":"ARIA/2.0"})
        with urlopen(req, timeout=5) as r: data = json.loads(r.read())
        return [m["name"] for m in data.get("models",[])]
    except: return []

# ── Session storage ────────────────────────────────────────────────────────────
def session_new(title: str = "New session") -> int:
    now = datetime.datetime.now().isoformat()
    cur = db().execute("INSERT INTO sessions(title,created_at,updated_at) VALUES(?,?,?)",
                       (title, now, now))
    db().commit()
    return cur.lastrowid

def session_add_message(session_id: int, role: str, content: str, metadata: dict = None):
    now = datetime.datetime.now().isoformat()
    cur = db().execute("INSERT INTO messages(session_id,role,content,ts,metadata) VALUES(?,?,?,?,?)",
                       (session_id, role, content, now, json.dumps(metadata or {})))
    db().execute("INSERT INTO messages_fts(rowid,content) VALUES(?,?)", (cur.lastrowid, content))
    db().execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
    db().commit()

def session_search(query: str, limit: int = 8) -> list:
    try:
        rows = db().execute("""
            SELECT m.id,m.session_id,m.role,m.content,m.ts,s.title
            FROM messages m JOIN messages_fts f ON m.id=f.rowid
            JOIN sessions s ON m.session_id=s.id
            WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?""", (query, limit)).fetchall()
        return [dict(r) for r in rows]
    except:
        rows = db().execute("""
            SELECT m.id,m.session_id,m.role,m.content,m.ts,s.title
            FROM messages m JOIN sessions s ON m.session_id=s.id
            WHERE m.content LIKE ? LIMIT ?""", (f"%{query}%", limit)).fetchall()
        return [dict(r) for r in rows]

def sessions_list(limit: int = 20) -> list:
    rows = db().execute(
        "SELECT id,title,created_at,updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]

# ── Tool execution ─────────────────────────────────────────────────────────────
def run_tool(tool: str, params: dict, model: str, ollama_url: str) -> dict:
    """Execute one of ARIA's built-in tools."""

    if tool == "memory":
        return memory_action(params.get("target","memory"), params.get("action","list"),
                             params.get("content",""), params.get("old_text",""))

    elif tool == "knowledge_search":
        q = params.get("query","")
        vec = rag_retrieve(q, params.get("top_k",5))
        fts = knowledge_fts_search(q, 5)
        combined = {r["id"]: r for r in vec}
        for r in fts: combined.setdefault(r["id"], r)
        return {"results": list(combined.values())[:6]}

    elif tool == "wikipedia":
        result = wikipedia_fetch(params.get("topic",""))
        return result or {"error": "not found"}

    elif tool == "web_search":
        results = ddg_search(params.get("query",""))
        # also persist results
        for r in results:
            if r.get("content"):
                knowledge_add(r.get("title","web result"), r["content"], "web_search", r.get("url",""))
        return {"results": results}

    elif tool == "execute_code":
        lang = params.get("language","python")
        code = params.get("code","")
        timeout = min(params.get("timeout",15), 30)
        try:
            if lang == "python":
                result = subprocess.run(["python3","-c",code],
                    capture_output=True, text=True, timeout=timeout)
            elif lang in ("bash","shell","sh"):
                result = subprocess.run(["bash","-c",code],
                    capture_output=True, text=True, timeout=timeout)
            else:
                return {"error": f"unsupported language: {lang}"}
            return {"stdout": result.stdout[:2000], "stderr": result.stderr[:500],
                    "returncode": result.returncode}
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {timeout}s"}
        except Exception as e:
            return {"error": str(e)}

    elif tool == "read_file":
        try:
            p = Path(params.get("path","")).expanduser()
            return {"content": p.read_text()[:4000]}
        except Exception as e: return {"error": str(e)}

    elif tool == "write_file":
        try:
            p = Path(params.get("path","")).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(params.get("content",""))
            return {"ok": True, "path": str(p)}
        except Exception as e: return {"error": str(e)}

    elif tool == "skill_create":
        return skill_create(params.get("name",""), params.get("content",""))

    elif tool == "skill_patch":
        return skill_patch(params.get("name",""), params.get("old_str",""), params.get("new_str",""))

    elif tool == "skill_delete":
        return skill_delete(params.get("name",""))

    elif tool == "skill_view":
        content = skill_view(params.get("name",""))
        return {"content": content} if content else {"error": "not found"}

    elif tool == "skills_list":
        return {"skills": skills_list()}

    elif tool == "session_search":
        return {"results": session_search(params.get("query",""), params.get("limit",8))}

    elif tool == "cron_add":
        try:
            db().execute("INSERT OR REPLACE INTO cron_jobs(name,schedule,prompt,enabled) VALUES(?,?,?,1)",
                         (params["name"], params["schedule"], params["prompt"]))
            db().commit()
            return {"ok": True}
        except Exception as e: return {"error": str(e)}

    elif tool == "cron_list":
        rows = db().execute("SELECT * FROM cron_jobs").fetchall()
        return {"jobs": [dict(r) for r in rows]}

    # ── Coding research tools ─────────────────────────────────────────────────
    elif tool == "stackoverflow_search":
        q = params.get("query","")
        results = stackoverflow_search(q, params.get("limit",5))
        for r in results:
            if r.get("content") and not r.get("error"):
                knowledge_add(r.get("title",q), r["content"], "stackoverflow", r.get("url",""))
        return {"results": results}

    elif tool == "stackoverflow_answers":
        return {"answers": stackoverflow_answers(str(params.get("question_id","")))}

    elif tool == "mdn_search":
        results = mdn_search(params.get("query",""))
        for r in results:
            if r.get("content"):
                knowledge_add(r.get("title",""), r["content"], "mdn", r.get("url",""))
        return {"results": results}

    elif tool == "pypi":
        result = pypi_search(params.get("package",""))
        if result.get("content"):
            knowledge_add(result.get("name",""), result["content"], "pypi", result.get("url",""))
        return result

    elif tool == "npm":
        result = npm_search(params.get("package",""))
        if result.get("content"):
            knowledge_add(result.get("name",""), result["content"], "npm", result.get("url",""))
        return result

    elif tool == "github_search":
        results = github_search_repos(params.get("query",""), params.get("limit",4))
        for r in results:
            if r.get("content"):
                knowledge_add(r.get("title",""), r["content"], "github", r.get("url",""))
        return {"results": results}

    elif tool == "github_readme":
        result = github_readme(params.get("repo",""))
        if result.get("content"):
            knowledge_add(f"README: {params.get('repo','')}", result["content"], "github_readme", result.get("url",""))
        return result

    elif tool == "devdocs":
        results = devdocs_search(params.get("query",""))
        for r in results:
            if r.get("content"):
                knowledge_add(r.get("title",""), r["content"], "devdocs", r.get("url",""))
        return {"results": results}

    elif tool == "caniuse":
        result = caniuse_search(params.get("feature",""))
        if result.get("content"):
            knowledge_add(result.get("title",""), result["content"], "caniuse", result.get("url",""))
        return result

    elif tool == "tldr":
        result = tldr_pages(params.get("command",""))
        if result.get("content"):
            knowledge_add(f"tldr: {result.get('command','')}", result["content"], "tldr", result.get("url",""))
        return result

    elif tool == "python_docs":
        results = python_docs_search(params.get("query",""))
        for r in results:
            if r.get("content"):
                knowledge_add(r.get("title",""), r["content"], "python_docs", r.get("url",""))
        return {"results": results}

    elif tool == "code_search":
        q = params.get("query","")
        sources = params.get("sources", ["stackoverflow","mdn","devdocs","github"])
        results = code_search_unified(q, sources)
        return results

    return {"error": f"unknown tool: {tool}"}

# ── Agentic loop (multi-step tool calling) ─────────────────────────────────────
TOOLS_SCHEMA = """You have these tools. Call ONE at a time using EXACTLY this JSON format on its own line:
TOOL_CALL: {"tool":"<name>","params":{...}}

GENERAL TOOLS:
- memory: action=add/remove/replace/list, target=memory|user, content=..., old_text=...
- knowledge_search: query=...  (search local knowledge DB first)
- wikipedia: topic=...
- web_search: query=...  (DuckDuckGo)
- execute_code: language=python|bash, code=...
- read_file: path=...
- write_file: path=..., content=...
- skill_create: name=..., content=... (full SKILL.md)
- skill_patch: name=..., old_str=..., new_str=...
- skill_delete: name=...
- skill_view: name=...
- skills_list: {}
- session_search: query=...
- cron_add: name=..., schedule="every 6h"|"daily at 9am", prompt=...

CODING RESEARCH TOOLS (use these for any programming questions):
- stackoverflow_search: query=...  (search Stack Overflow Q&A)
- stackoverflow_answers: question_id=...  (get top answers for a specific SO question ID)
- mdn_search: query=...  (MDN Web Docs — HTML/CSS/JS/Web APIs)
- devdocs: query=...  (DevDocs — aggregates Python, JS, CSS, Bash, Go, Rust, Node docs)
- python_docs: query=...  (Python official documentation)
- pypi: package=...  (PyPI package info — Python)
- npm: package=...  (npm package info — JavaScript/Node)
- github_search: query=..., limit=4  (search GitHub repos)
- github_readme: repo=owner/repo  (fetch repo README)
- caniuse: feature=...  (CSS/HTML/JS browser compatibility)
- tldr: command=...  (simplified man pages for CLI commands)
- code_search: query=..., sources=["stackoverflow","mdn","devdocs","github"]  (parallel multi-source search)

CODING STRATEGY:
1. For any coding question, FIRST check knowledge_search (may already have the answer)
2. Use code_search for broad questions hitting multiple sources at once
3. Use stackoverflow_search for error messages, "how to" questions, language-specific problems
4. Use mdn_search for HTML/CSS/JS/Web API questions
5. Use devdocs for language reference docs (Python stdlib, Node.js, CSS properties, etc)
6. Use pypi/npm to check package existence, version, and compatibility
7. Use tldr for CLI command syntax and examples
8. Always execute_code to verify code snippets actually work before sending to user
9. After solving a complex coding task, create a skill document for future reuse

IMPORTANT: After all tool calls are done, write your final answer as plain text (no TOOL_CALL prefix).
Proactively use memory(add) to save user language preferences, project stack, coding style."""

def agentic_chat(messages: list, model: str, ollama_url: str,
                 session_id: int, max_steps: int = 8) -> dict:
    """Run the multi-step agentic loop with tool calling."""
    system = build_system_prompt() + "\n\n" + TOOLS_SCHEMA

    # RAG injection
    query = messages[-1]["content"] if messages else ""
    relevant = rag_retrieve(query)
    if relevant:
        ctx = "\n\n[RESEARCH CONTEXT]\n"
        ctx += "\n\n".join(f"{r['topic']}: {r['content'][:400]}" for r in relevant)
        ctx += "\n[/RESEARCH CONTEXT]"
        augmented = messages[:-1] + [{"role":"user","content": messages[-1]["content"]+ctx}]
    else:
        augmented = messages

    tool_log = []
    current_messages = augmented.copy()

    for step in range(max_steps):
        reply = ollama_chat(current_messages, system, model, ollama_url, 1200)

        # Parse tool calls
        tool_calls = re.findall(r'TOOL_CALL:\s*(\{.*?\})', reply, re.DOTALL)
        if not tool_calls:
            # Final answer
            return {"reply": reply, "tool_log": tool_log,
                    "used_context": [r["topic"] for r in relevant]}

        # Execute each tool call
        tool_results = []
        for tc_str in tool_calls:
            try:
                tc = json.loads(tc_str)
                tool_name = tc.get("tool","")
                params = tc.get("params", {})
                result = run_tool(tool_name, params, model, ollama_url)
                tool_log.append({"tool": tool_name, "params": params, "result": result})
                tool_results.append(f"[{tool_name}] → {json.dumps(result)[:500]}")
            except Exception as e:
                tool_results.append(f"[tool error] {e}")

        # Feed results back
        current_messages = current_messages + [
            {"role": "assistant", "content": reply},
            {"role": "user",      "content": "Tool results:\n" + "\n".join(tool_results) + "\n\nContinue."}
        ]

    return {"reply": "Reached max steps.", "tool_log": tool_log,
            "used_context": [r["topic"] for r in relevant]}

# ── Self-evaluation & evolution ────────────────────────────────────────────────
def load_evo() -> dict:
    if EVO_FILE.exists():
        try: return json.loads(EVO_FILE.read_text())
        except: pass
    return {"generation":1,"log":[],"scores":[]}

def save_evo(e: dict): EVO_FILE.write_text(json.dumps(e, indent=2))

def evolve(messages: list, model: str, ollama_url: str) -> dict:
    if len(messages) < 4:
        return {"status":"skipped","reason":"Need more conversation first."}

    evo = load_evo()
    recent = "\n".join(f"{m['role'].upper()}: {m['content'][:200]}" for m in messages[-8:])
    eval_prompt = (
        "Rate this AI conversation 1-10. Consider: helpfulness, tool use, memory management, skill creation. "
        "Return ONLY valid JSON: {\"score\":7,\"reason\":\"brief\",\"improvements\":[\"fix1\",\"fix2\"]}\n\n"
        + recent)
    try:
        raw = ollama_chat([{"role":"user","content":eval_prompt}],
                         "You evaluate AI conversations. Return only valid JSON, nothing else.",
                         model, ollama_url, 300)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        evaluation = json.loads(m.group()) if m else None
    except: evaluation = None

    if not evaluation:
        return {"status":"error","reason":"Could not parse evaluation"}

    score = evaluation.get("score",5)
    evo["scores"].append(score)
    evo["scores"] = evo["scores"][-10:]

    if score < 8 or len(evo["scores"]) >= 5:
        improve_prompt = (
            f"Current ARIA soul/system prompt scores {score}/10.\n"
            f"Issues: {'; '.join(evaluation.get('improvements',[]))}\n\n"
            f"Current soul:\n{load_soul()}\n\n"
            f"Known topics: {', '.join([r['topic'] for r in knowledge_fts_search('',5)])}\n\n"
            "Write an improved SOUL.md. Under 250 words. Return ONLY the new text.")
        try:
            new_soul = ollama_chat([{"role":"user","content":improve_prompt}],
                                   "You are a prompt engineer. Return only the soul prompt text.",
                                   model, ollama_url, 400)
            if new_soul.strip():
                save_soul(new_soul.strip())
                evo["generation"] += 1
                evo["scores"] = []
                evo["log"].insert(0, {
                    "generation": evo["generation"],
                    "score": score,
                    "reason": evaluation.get("reason",""),
                    "improvements": evaluation.get("improvements",[]),
                    "timestamp": datetime.datetime.now().isoformat()
                })
                evo["log"] = evo["log"][:50]
                save_evo(evo)
                return {"status":"evolved","generation":evo["generation"],"score":score,
                        "new_soul":new_soul[:200]}
        except Exception as e:
            return {"status":"error","reason":str(e)}

    save_evo(evo)
    return {"status":"evaluated","score":score,"reason":evaluation.get("reason",""),
            "generation":evo["generation"]}

# ── Extract topics for auto-research ──────────────────────────────────────────
def extract_topics(messages: list, model: str, ollama_url: str) -> list[str]:
    if len(messages) < 2: return []
    recent = "\n".join(m["content"][:150] for m in messages[-6:])
    try:
        raw = ollama_chat(
            [{"role":"user","content":f"Extract 2-3 specific factual topics from this conversation for Wikipedia research. Return ONLY a JSON array of short strings.\n\n{recent}"}],
            "Extract topics. Return only a JSON array, nothing else.", model, ollama_url, 150)
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        return json.loads(m.group())[:3] if m else []
    except: return []

# ── Cron runner ────────────────────────────────────────────────────────────────
def parse_schedule(schedule: str) -> int:
    """Return interval seconds from human schedule."""
    s = schedule.lower()
    if "hourly" in s: return 3600
    if "daily" in s: return 86400
    if "weekly" in s: return 604800
    m = re.search(r'every\s+(\d+)\s*(h|hr|hour)', s)
    if m: return int(m.group(1))*3600
    m = re.search(r'every\s+(\d+)\s*(m|min)', s)
    if m: return int(m.group(1))*60
    return 3600

def cron_worker():
    import time as _time
    while True:
        _time.sleep(60)
        try:
            jobs = db().execute("SELECT * FROM cron_jobs WHERE enabled=1").fetchall()
            now = datetime.datetime.now()
            for job in jobs:
                last = job["last_run"]
                interval = parse_schedule(job["schedule"])
                if last:
                    delta = (now - datetime.datetime.fromisoformat(last)).total_seconds()
                    if delta < interval: continue
                # Run the job — store result in knowledge
                # (would need a model/url config — skip actual LLM call in worker for safety)
                db().execute("UPDATE cron_jobs SET last_run=? WHERE id=?",
                             (now.isoformat(), job["id"]))
                db().commit()
        except: pass

threading.Thread(target=cron_worker, daemon=True).start()

# ── HTTP Handler ───────────────────────────────────────────────────────────────
class ARIAHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=str(STATIC), **kw)
    def log_message(self, *a): pass

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")

    def do_GET(self):
        path = urlparse(self.path).path
        qs   = parse_qs(urlparse(self.path).query)
        def q(k,d=""): return qs.get(k,[d])[0]

        if path == "/api/status":
            evo = load_evo()
            kcount = db().execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            scount = db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            mcount = db().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            kb = DB_PATH.stat().st_size//1024 if DB_PATH.exists() else 0
            models = ollama_models(q("url",DEFAULT_OLLAMA))
            self._json({
                "ollama_ok": bool(models), "models": models,
                "generation": evo["generation"],
                "knowledge_count": kcount, "session_count": scount,
                "message_count": mcount, "db_kb": kb,
                "skills": len(skills_list()),
                "memory": memory_load()
            })
        elif path == "/api/memory":
            self._json(memory_load())
        elif path == "/api/skills":
            self._json({"skills": skills_list()})
        elif path == "/api/skill":
            name = q("name")
            content = skill_view(name)
            self._json({"content": content} if content else {"error":"not found"})
        elif path == "/api/sessions":
            self._json({"sessions": sessions_list()})
        elif path == "/api/evolution":
            self._json(load_evo())
        elif path == "/api/knowledge":
            limit = int(q("limit","30"))
            rows = db().execute("SELECT id,topic,source,url,created_at FROM knowledge ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            self._json({"items": [dict(r) for r in rows]})
        elif path == "/api/models":
            self._json({"models": ollama_models(q("url",DEFAULT_OLLAMA))})
        elif path == "/api/ping":
            model = q("model", "")
            url   = q("url", DEFAULT_OLLAMA)
            if not model:
                self._json({"ready": False, "error": "no model selected"}); return
            # Fast path: check /api/ps (no inference cost)
            try:
                ps_req = Request(f"{url}/api/ps", headers={"User-Agent":"ARIA/2.0"})
                with urlopen(ps_req, timeout=3) as r:
                    ps_data = json.loads(r.read())
                if any(m["name"] == model for m in ps_data.get("models", [])):
                    self._json({"ready": True}); return
            except: pass
            # Slow path: tiny generate forces load and confirms the model responds
            try:
                payload = json.dumps({
                    "model": model, "prompt": "hi", "stream": False,
                    "options": {"num_predict": 1}
                }).encode()
                req = Request(f"{url}/api/generate", data=payload,
                              headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(req, timeout=60) as r: json.loads(r.read())
                self._json({"ready": True})
            except Exception as e:
                self._json({"ready": False, "error": str(e)})
        elif path == "/api/soul":
            self._json({"soul": load_soul()})
        else:
            super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length",0))
        body = json.loads(self.rfile.read(length)) if length else {}
        path = urlparse(self.path).path

        dispatch = {
            "/api/chat":            self._chat,
            "/api/research":        self._research,
            "/api/evolve":          self._evolve,
            "/api/extract_topics":  self._extract_topics,
            "/api/memory":          self._memory,
            "/api/skill":           self._skill,
            "/api/knowledge/add":   self._knowledge_add,
            "/api/knowledge/search":self._knowledge_search,
            "/api/soul":            self._soul,
            "/api/tool":            self._tool,
            "/api/code_search":     self._code_search,
            "/api/so_answers":      self._so_answers,
        }
        handler = dispatch.get(path)
        if handler: handler(body)
        else: self.send_response(404); self.end_headers()

    def _chat(self, body):
        messages   = body.get("messages",[])
        model      = body.get("model","llama3.2")
        ollama_url = body.get("ollama_url", DEFAULT_OLLAMA)
        session_id = body.get("session_id")

        if not session_id:
            title = messages[-1]["content"][:40] if messages else "Chat"
            session_id = session_new(title)

        # Save user message
        if messages:
            session_add_message(session_id, "user", messages[-1]["content"])

        try:
            result = agentic_chat(messages, model, ollama_url, session_id)
            session_add_message(session_id, "assistant", result["reply"],
                                {"tool_log": result.get("tool_log",[]),
                                 "used_context": result.get("used_context",[])})

            # Auto-research every 3 turns
            turn_count = db().execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='assistant'",
                (session_id,)).fetchone()[0]
            if turn_count % 3 == 0:
                topics = extract_topics(messages, model, ollama_url)
                for t in topics[:2]:
                    wikipedia_fetch(t)

            self._json({**result, "session_id": session_id})
        except URLError as e:
            self._json({"error": f"Cannot reach Ollama. Is it running? ({e})"}, 502)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _research(self, body):
        topics  = body.get("topics",[])
        results = []
        for t in topics[:4]:
            r = wikipedia_fetch(t)
            if r: results.append(r)
        self._json({"researched": results,
                    "knowledge_count": db().execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]})

    def _evolve(self, body):
        result = evolve(body.get("messages",[]),
                        body.get("model","llama3.2"),
                        body.get("ollama_url",DEFAULT_OLLAMA))
        self._json(result)

    def _extract_topics(self, body):
        topics = extract_topics(body.get("messages",[]),
                                body.get("model","llama3.2"),
                                body.get("ollama_url",DEFAULT_OLLAMA))
        self._json({"topics": topics})

    def _memory(self, body):
        result = memory_action(body.get("target","memory"), body.get("action","list"),
                               body.get("content",""), body.get("old_text",""))
        self._json(result)

    def _skill(self, body):
        action = body.get("action","list")
        if action == "create":
            self._json(skill_create(body.get("name",""), body.get("content","")))
        elif action == "patch":
            self._json(skill_patch(body.get("name",""), body.get("old_str",""), body.get("new_str","")))
        elif action == "delete":
            self._json(skill_delete(body.get("name","")))
        elif action == "view":
            c = skill_view(body.get("name",""))
            self._json({"content": c} if c else {"error":"not found"})
        else:
            self._json({"skills": skills_list()})

    def _knowledge_add(self, body):
        result = knowledge_add(body.get("topic","Note"), body.get("content",""),
                               body.get("source","manual"), body.get("url",""))
        self._json(result)

    def _knowledge_search(self, body):
        q = body.get("query","")
        vec = rag_retrieve(q, body.get("top_k",6))
        fts = knowledge_fts_search(q, 6)
        combined = {r["id"]: r for r in vec}
        for r in fts: combined.setdefault(r["id"], r)
        self._json({"results": list(combined.values())[:8]})

    def _soul(self, body):
        if "soul" in body:
            save_soul(body["soul"])
            self._json({"ok": True})
        else:
            self._json({"soul": load_soul()})

    def _tool(self, body):
        result = run_tool(body.get("tool",""), body.get("params",{}),
                          body.get("model","llama3.2"),
                          body.get("ollama_url",DEFAULT_OLLAMA))
        self._json(result)

    def _code_search(self, body):
        query   = body.get("query","")
        sources = body.get("sources", ["stackoverflow","mdn","devdocs","github"])
        results = code_search_unified(query, sources)
        self._json(results)

    def _so_answers(self, body):
        qid = str(body.get("question_id",""))
        self._json({"answers": stackoverflow_answers(qid)})

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(body))
        self.end_headers(); self.wfile.write(body)

class ThreadedServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

def main():
    port = int(os.environ.get("ARIA_PORT",7842))
    server = ThreadedServer(("127.0.0.1", port), ARIAHandler)
    print(f"""
╔══════════════════════════════════════════════════════╗
║  ARIA v2 — Local AI Agent                           ║
║  http://localhost:{port}                                ║
║                                                      ║
║  Data:   {str(DATA)[:42]}  ║
║  DB:     {str(DB_PATH)[:42]}  ║
║  Skills: {str(SKILLS_D)[:42]}  ║
║  Stop:   Ctrl+C                                      ║
╚══════════════════════════════════════════════════════╝
""")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nARIA stopped.")

if __name__ == "__main__": main()
