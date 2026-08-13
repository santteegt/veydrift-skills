# Repo-level convenience targets. Neither skill needs this to run or test on its own --
# see AGENTS.md §3 for the direct `uv`/`npm` commands each skill uses individually.

.PHONY: help clean install-skills

# Build-artifact directory (and file) names that `npx skills add` copies verbatim into
# .claude/skills/ and .agents/ if left lying around -- it does not honour .gitignore, so
# these must not exist under skills/ at install time. A copied .venv in particular is
# actively broken (dyld: Library not loaded) until deleted and rebuilt. See .gitignore's
# "Installed skill copies" note and docs/PLAYER-GUIDE.md §4.
ARTIFACT_DIR_NAMES := .venv node_modules dist __pycache__ .pytest_cache .ruff_cache *.egg-info

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/:/' | column -t -s ':'

clean: ## Remove .venv/node_modules/dist/__pycache__/etc. from skills/ so installing doesn't copy them
	@find skills \( $(foreach n,$(ARTIFACT_DIR_NAMES),-name '$(n)' -o) -false \) -prune -exec rm -rf {} +
	@find skills -name '*.tsbuildinfo' -delete
	@echo "Cleaned build artifacts from skills/veydrift-agent and skills/veydrift-wallet."

install-skills: clean ## Clean, then install both skills for this machine's Claude Code / Hermes config
	npx skills add . -g -a claude-code -a hermes-agent -y
