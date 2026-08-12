# skills/veydrift-agent/Makefile
#
# Minimal, non-authoritative convenience targets. `uv run ...` (docs/SPEC.md §0.1) is
# the actual zero-install entrypoint for everything here; this file just saves typing.

.PHONY: schemas test

schemas:
	uv run --directory . python scripts/generate_schemas.py

test:
	uv run --directory . pytest
