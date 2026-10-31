# Sourced by login/interactive shells and via BASH_ENV: put the agent rm guard first (incident 504d662b).
_g="$HOME/.local/share/agent-hub-ops/guard-bin"
case ":$PATH:" in *":$_g:"*) ;; *) [ -x "$_g/rm" ] && PATH="$_g:$PATH" ;; esac
export PATH; unset _g
