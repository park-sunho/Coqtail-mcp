# Coqtail-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude
Code, Codex, etc.) drive a live Rocq / Coq proof session: start a session,
step through a `.v` file, inspect the current goal and context, and fire
one-off queries like `Check`, `Print`, or `Search`.

Under the hood it talks to `coqidetop` / `coqtop` using the same XML protocol
that the [Coqtail](https://github.com/whonore/Coqtail) vim plugin uses. The
XML plumbing is not rewritten — three of Coqtail's Python modules
(`xmlInterface.py`, `coqtop.py`, `coqtail.py`) are vendored verbatim under
`src/coqtail_mcp/coqtail_lib/` and driven by a thin session layer.

## Why reuse Coqtail?

The hard parts — version-aware encoders/decoders (`XMLInterface84` through
`XMLInterface92`), framing, richpp parsing, sentence boundary detection
(including bullets, attributes, `lp:{{ }}` elpi blocks, nested comments) —
already work. This project only adds:

- a single-process-per-session wrapper (`RocqSession`)
- a session registry with thread-safe access
- a goal formatter that strips highlight tags for plain-text display
- a FastMCP server registering seven tools

## Tools

| Tool | What it does |
|------|--------------|
| `rocq_start`   | Spawn a `coqidetop` subprocess. Accepts either `file_path` or inline `content`. Returns the detected Rocq version. |
| `rocq_close`   | Terminate a session's subprocess and forget it. |
| `rocq_step_to` | Advance or rewind so the session's state matches `(line, col)`. Optionally re-reads the original `file_path` from disk (`reload_from_file`) and/or admits opaque proofs (`admit`). |
| `rocq_goals`   | Return the current proof goal and hypothesis context, both as plain text and as a structured summary. |
| `rocq_query`   | Run a non-state-changing query (`Check`, `Print`, `Search`, …). |
| `rocq_status`  | Inspect one session (version, sentence count, endpoint). |
| `rocq_list`    | List active session ids. |

All line and column numbers at the tool boundary are **1-indexed**. Every
state-reading tool also returns a rolled-up `info` field mirroring
Coqtail's info panel.

## Requirements

- Python ≥ 3.10
- `mcp` SDK (installed automatically via `pyproject.toml`)
- A working Rocq / Coq install on `$PATH`, or the path explicitly supplied via
  `coq_path` / `coq_prog` on `rocq_start`

## Install

```bash
cd Coqtail-mcp
pip install -e .
```

or with `uv`:

```bash
cd Coqtail-mcp
uv pip install -e .
```

This installs a `coqtail-mcp` command that runs the MCP server over stdio.

## Claude Code configuration

Register the server with Claude Code by editing `~/.claude.json` (or the
project-scoped `.claude.json`):

```json
{
  "mcpServers": {
    "coqtail": {
      "command": "coqtail-mcp"
    }
  }
}
```

If you didn't install with `pip install -e .`, point at the module instead:

```json
{
  "mcpServers": {
    "coqtail": {
      "command": "python",
      "args": ["-m", "coqtail_mcp"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/Coqtail-mcp/src"
      }
    }
  }
}
```

An example config lives at `examples/mcp_config.json`.

## Skill (optional, recommended)

A Claude Code skill that teaches the agent how to drive this server is
bundled under `skills/coqtail-mcp/`. It spells out the session lifecycle,
position conventions, error envelope, and a set of workflow recipes.

To install it system-wide:

```bash
ln -s "$(pwd)/skills/coqtail-mcp" ~/.claude/skills/coqtail-mcp
```

or project-locally (inside the repo the agent is working on):

```bash
ln -s "/abs/path/to/Coqtail-mcp/skills/coqtail-mcp" \
      .claude/skills/coqtail-mcp
```

With the skill active, Claude will automatically follow server-specific
guidance (e.g. "leave `coq_prog` blank on Rocq ≥ 8.9") without you
having to prompt for it.

## Example session (pseudocode for the agent)

```
rocq_start(session_id="demo", file_path="demo.v")
# → { version: { str_version: "9.0.0", … } }

rocq_step_to(session_id="demo", line=6)
# → { success: true, sentences_applied: 4, endpoint: [6, 7] }

rocq_goals(session_id="demo")
# → {
#     text: "1 subgoal\n\nn : nat\n\n========================= (1 / 1)\n\nn + 0 = n\n",
#     summary: { in_proof: true, fg: [...], ... }
#   }

rocq_query(session_id="demo", query="Check nat")
# → { message: "nat : Set", success: true }

rocq_close(session_id="demo")
```

For `file_path` sessions, `rocq_start` automatically searches upward for
project settings. The default `build_system="prefer-dune"` uses Dune when a
`dune-project` file is present, otherwise it reads `_CoqProject` and
`_RocqProject` flags. Use `build_system="prefer-coqproject"`, `"dune"`, or
`"coqproject"` to force the selection, and pass `extra_args` for final
overrides.

## Project layout

```
Coqtail-mcp/
├── src/coqtail_mcp/
│   ├── __init__.py          # sys.path shim so vendored modules resolve
│   ├── __main__.py          # `python -m coqtail_mcp`
│   ├── server.py            # FastMCP server + tool definitions
│   ├── session.py           # RocqSession + SessionRegistry
│   ├── project.py           # _CoqProject / Dune discovery
│   ├── formatting.py        # Goals → plain text / structured summary
│   └── coqtail_lib/         # vendored Coqtail modules (unmodified)
│       ├── xmlInterface.py
│       ├── coqtop.py
│       └── coqtail.py
├── tests/
│   ├── test_project.py      # project discovery/parser tests
│   └── test_session.py      # offline + live smoke tests
├── examples/
│   ├── demo.v               # sample Coq file used by tests
│   └── mcp_config.json      # sample Claude Code config
├── skills/
│   └── coqtail-mcp/         # Claude Code skill for this server
│       ├── SKILL.md
│       └── references/
├── pyproject.toml
├── LICENSE
└── README.md
```

## Running the smoke tests

Offline tests (buffer parsing, registry) run without Rocq:

```bash
python -m pytest tests/ -k "offline"
```

Live tests spawn a real Rocq process and require `coqtop`/`coqidetop` or
`rocq` on `$PATH`:

```bash
python -m pytest tests/
# or to point at a specific opam switch:
COQ_PATH=/home/you/.opam/my-switch/bin python -m pytest tests/
```

## Credits

The XML-protocol client is Coqtail
([whonore/Coqtail](https://github.com/whonore/Coqtail), MIT). See `LICENSE`
for attribution.
