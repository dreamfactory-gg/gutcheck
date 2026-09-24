# gutcheck long-run mode. Source from ~/.zshrc:  source /path/to/gutcheck/shell/long-run.zsh
#   claude ...  starts sessions with the long-run prompt (long-run.md, in the system prompt from the first
#               request) and CLAUDE_LONG_RUN=1, which turns on the Stop check.
#   short ...   the plain binary: no long-run prompt, no Stop enforcement. All other hooks and skills stay on.
# Subcommands (mcp, plugin, update, ...) pass straight through.

typeset -g GUTCHECK_LONG_RUN_MD="${${(%):-%x}:A:h:h}/long-run.md"

claude() {
  case "$1" in
    agents|attach|auth|auto-mode|doctor|gateway|import|install|logs|mcp|plugin|plugins|project|respawn|rm|\
    setup-token|stop|kill|ultrareview|update|upgrade|config|migrate-installer)
      command claude "$@"; return ;;
  esac
  if [[ -r "$GUTCHECK_LONG_RUN_MD" ]]; then
    CLAUDE_LONG_RUN=1 command claude --append-system-prompt-file "$GUTCHECK_LONG_RUN_MD" "$@"
  else
    command claude "$@"
  fi
}

alias short='CLAUDE_LONG_RUN=0 command claude'
