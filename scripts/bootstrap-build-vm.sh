#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu Lightsail/EC2 instance for building the Nuitka image.
#
# Usage (on a fresh Ubuntu 24.04 instance, as the default `ubuntu` user):
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/<branch>/scripts/bootstrap-build-vm.sh | bash
# or:
#   scp scripts/bootstrap-build-vm.sh ubuntu@<host>:~ && ssh ubuntu@<host> 'bash ~/bootstrap-build-vm.sh'
#
# After it finishes:
#   cd ~/autobots && make nuitka-build

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/jetoslabs/autobots.git}"
REPO_DIR="${REPO_DIR:-$HOME/autobots}"
BRANCH="${BRANCH:-main}"

log() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

log "Updating apt"
sudo apt-get update -y

log "Installing base packages (git, make, curl, ca-certificates, build basics, python, zsh)"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git \
    make \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    htop \
    unzip \
    vim \
    jq \
    zsh \
    zsh-autosuggestions \
    python3 \
    python3-venv \
    python3-pip \
    pipx

log "Installing Docker (with BuildKit / buildx)"
if ! command -v docker >/dev/null 2>&1; then
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin
else
    log "Docker already installed, skipping"
fi

log "Enabling Docker service"
sudo systemctl enable --now docker

log "Adding $USER to docker group (effective on next login)"
sudo usermod -aG docker "$USER" || true

log "Enabling BuildKit by default"
sudo mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
    echo '{ "features": { "buildkit": true } }' | sudo tee /etc/docker/daemon.json >/dev/null
    sudo systemctl restart docker
fi

log "Installing uv"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
else
    log "uv already installed, skipping"
fi

log "Installing Node.js 20 (NodeSource) for Claude Code"
if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs
else
    log "Node.js already installed, skipping"
fi

log "Installing Claude Code CLI"
if ! command -v claude >/dev/null 2>&1; then
    sudo npm install -g @anthropic-ai/claude-code
else
    log "Claude Code already installed, skipping"
fi

log "Setting up zsh as default shell + writing ~/.zshrc"
if [[ "$(getent passwd "$USER" | cut -d: -f7)" != "$(command -v zsh)" ]]; then
    sudo chsh -s "$(command -v zsh)" "$USER"
fi

if [[ ! -f "$HOME/.zshrc" ]]; then
    cat > "$HOME/.zshrc" <<'ZSHRC'
# ~/.zshrc — modern defaults

# ---- History ----
HISTFILE=~/.zsh_history
HISTSIZE=100000
SAVEHIST=100000
setopt APPEND_HISTORY INC_APPEND_HISTORY SHARE_HISTORY
setopt HIST_IGNORE_DUPS HIST_IGNORE_ALL_DUPS HIST_IGNORE_SPACE
setopt HIST_REDUCE_BLANKS HIST_VERIFY

# ---- Navigation ----
setopt AUTO_CD AUTO_PUSHD PUSHD_IGNORE_DUPS PUSHD_SILENT
setopt EXTENDED_GLOB INTERACTIVE_COMMENTS

# ---- Completion ----
autoload -Uz compinit && compinit
zstyle ':completion:*' menu select
zstyle ':completion:*' matcher-list 'm:{a-zA-Z}={A-Za-z}' 'r:|=*' 'l:|=* r:|=*'

# ---- Key bindings ----
bindkey -e
bindkey '^[[A' history-search-backward
bindkey '^[[B' history-search-forward
bindkey '^[[1;5C' forward-word
bindkey '^[[1;5D' backward-word

# ---- Prompt with git branch ----
autoload -Uz vcs_info
precmd() { vcs_info }
zstyle ':vcs_info:git:*' formats ' (%b)'
setopt PROMPT_SUBST
PROMPT='%F{cyan}%n@%m%f:%F{yellow}%~%f%F{green}${vcs_info_msg_0_}%f %# '

# ---- Aliases ----
alias ll='ls -lah'
alias la='ls -A'
alias l='ls -CF'
alias ..='cd ..'
alias ...='cd ../..'
alias gs='git status'
alias gd='git diff'
alias gl='git log --oneline --graph --decorate'

# ---- Environment ----
export EDITOR=vim
export PAGER=less
export LESS='-R -F -X'
export PATH="$HOME/.local/bin:$PATH"

# ---- Autosuggestions ----
if [[ -f /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh ]]; then
    source /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh
    ZSH_AUTOSUGGEST_STRATEGY=(history completion)
    ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE='fg=8'
    bindkey '^ ' autosuggest-accept
fi

[[ -f ~/.zshrc.local ]] && source ~/.zshrc.local
ZSHRC
fi

log "Cloning repo: $REPO_URL ($BRANCH) -> $REPO_DIR"
if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" fetch --all --prune
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" pull --ff-only
else
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
fi

log "Done."
cat <<EOF

  Bootstrap complete.

  Next steps:
    1. Log out and back in (so the docker group takes effect):
         exit
         ssh ubuntu@<host>

    2. Build:
         cd $REPO_DIR
         make nuitka-build

  To override the repo or branch on next run:
    REPO_URL=...  BRANCH=...  bash bootstrap-build-vm.sh

EOF
