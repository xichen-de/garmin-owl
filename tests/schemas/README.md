# Agent Plugins schemas

Unmodified official Agent Plugins 1.0.0 schemas, downloaded on 2026-10-01:

- https://agent-plugins.org/schemas/1.0.0/plugin.schema.json
- https://agent-plugins.org/schemas/1.0.0/mcp.schema.json

Vendored for offline validation with `jsonschema` in `test_chatgpt_plugin.py`.
The normative specification at https://agent-plugins.org/specification also
defines runtime requirements that JSON Schema cannot check. OpenAI's extension
metadata and marketplace format are documented separately at
https://developers.openai.com/plugins/build/plugins.
