# Example vertical — RSS research agent

A fully filled-in vertical built with **zero changes to `agent/`**. Copy this
folder's shape to start your own.

What it shows:

| Seam | Here |
|------|------|
| Drop-in tool | [`tools/rss_fetch.py`](tools/rss_fetch.py) — one function, auto-discovered |
| `settings.yaml` | the `feeds:` list and `max_items` |
| State store | seen-URL dedup set, so items never repeat |
| Shared deps | uses `ctx.deps.http` + `ctx.deps.settings` + `parse_rss` (`from agent import parse_rss`) |
| `persona.md` | the research-assistant system prompt |
| `output_type` | [`brief.py`](brief.py) returns a typed `Briefing` |

## Run it

From the repo root, point the agent at this folder:

```bash
cd examples/rss_research
cp ../../.env.example .env     # set your provider/model/key (or use ollama)
uv run agent "Give me today's briefing"
```

Or get **structured** output:

```bash
cd examples/rss_research
uv run python brief.py
```

The `agent/` engine is never touched — this folder is only `persona.md`,
`settings.yaml`, and `tools/`.
