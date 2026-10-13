# Frankensurf plugin for Claude Code

Adds the Frankensurf MCP server (`read`, `search`, `extract` and the rest) and the skill that
teaches Claude when and how to use them.

The plugin starts `frankensurf-mcp`, so install the package first:

```bash
pip install "frankensurf[mcp]"     # or: uv tool install "frankensurf[mcp]"
frankensurf install                # the free browsers and the local search engine
```

Then in Claude Code:

```
/plugin marketplace add yail259/frankensurf
/plugin install frankensurf@frankensurf
```

The skill here is a copy of `src/frankensurf/skill/SKILL.md`; a test keeps them identical.
