#!/bin/sh
# Heftig installer for Linux and macOS: one container, one folder, nothing else on the system.
#
#   curl -fsSL https://raw.githubusercontent.com/jo-tud/heftig/main/install.sh | sh
#
# Options (as environment variables, e.g. `... | HEFTIG_LAN=1 sh`):
#   HEFTIG_HOME   where the archive and the scanner folder live   (default: ~/heftig)
#   HEFTIG_PORT   port of the web interface                       (default: 8765)
#   HEFTIG_LAN=1  reachable from other devices in the network     (default: this computer only)
#   HEFTIG_IMAGE  container image                                 (default: see IMAGE below)
#   HEFTIG_BUILD=1  build the image from the source instead of downloading it
#   HEFTIG_NAME   container (and service) name                    (default: heftig)
#
# What it does: checks for Podman or Docker, downloads the Heftig image, creates ~/heftig,
# starts the container (it restarts with the computer) and prints the address of the setup page.
# Running it again updates Heftig and keeps all data.
set -eu

REPO="jo-tud/heftig"
IMAGE="${HEFTIG_IMAGE:-ghcr.io/jo-tud/heftig:latest}"
HOME_DIR="${HEFTIG_HOME:-$HOME/heftig}"
PORT="${HEFTIG_PORT:-8765}"
NAME="${HEFTIG_NAME:-heftig}"

say() { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
ask() {  # ask "Question?" -> 0 for yes (works with curl | sh: reads from the terminal)
    [ -r /dev/tty ] || return 1
    printf '%s [y/N] ' "$1" > /dev/tty
    read -r answer < /dev/tty || return 1
    case "$answer" in [yYjJ]*) return 0 ;; *) return 1 ;; esac
}

# everything runs from main(), called on the last line: a download cut off halfway does nothing
main() {
    OS=$(uname -s)
    case "$OS" in Linux | Darwin) ;; *) die "This installer is for Linux and macOS. See the README for other systems." ;; esac
    command -v curl > /dev/null 2>&1 || die "Please install curl first."
    [ "$(id -u)" != "0" ] || say "Note: running as root. Heftig's files will belong to root; a normal user is recommended."

    # --- container engine ------------------------------------------------------------------------
    step "1/4  Container engine"
    ENGINE=""
    if command -v podman > /dev/null 2>&1; then
        ENGINE=podman
    elif command -v docker > /dev/null 2>&1; then
        ENGINE=docker
    fi
    if [ -z "$ENGINE" ] && [ "$OS" = Darwin ]; then
        die "Heftig runs in a container and needs Docker. Install Docker Desktop (https://www.docker.com/products/docker-desktop/), OrbStack or Colima, start it and run this installer again."
    fi
    if [ -z "$ENGINE" ]; then
        install_cmd=""
        if command -v dnf > /dev/null 2>&1; then install_cmd="sudo dnf install -y podman"
        elif command -v apt-get > /dev/null 2>&1; then install_cmd="sudo apt-get update && sudo apt-get install -y podman"
        elif command -v zypper > /dev/null 2>&1; then install_cmd="sudo zypper install -y podman"
        elif command -v pacman > /dev/null 2>&1; then install_cmd="sudo pacman -S --noconfirm podman"
        fi
        say "Heftig runs in a container and needs Podman (recommended) or Docker."
        [ -n "$install_cmd" ] || die "Please install Podman (https://podman.io) and run this installer again."
        say "It can be installed with:  $install_cmd"
        if ask "Install Podman now (asks for your password)?"; then
            sh -c "$install_cmd" || die "Installing Podman failed."
            ENGINE=podman
        else
            die "Please install Podman and run this installer again."
        fi
    fi
    say "Using $ENGINE ($($ENGINE --version 2>/dev/null | head -n1))."
    if [ "$ENGINE" = docker ] && ! docker info > /dev/null 2>&1; then
        die "Docker is installed but not usable by $(id -un) (is the service running, is the user in the 'docker' group?)."
    fi
    # on macOS, Podman runs its containers in a virtual machine that must be started first
    if [ "$ENGINE" = podman ] && [ "$OS" = Darwin ] && ! podman info > /dev/null 2>&1; then
        die "Podman is installed but its virtual machine is not running (podman machine init; podman machine start)."
    fi

    # --- folders ---------------------------------------------------------------------------------
    step "2/4  Folders"
    mkdir -p "$HOME_DIR/archive" "$HOME_DIR/scanner"
    chmod 700 "$HOME_DIR/archive"
    say "Archive:        $HOME_DIR/archive   (everything Heftig keeps: originals, text, database)"
    say "Scanner folder: $HOME_DIR/scanner   (files put here are imported automatically)"

    # --- image -----------------------------------------------------------------------------------
    step "3/4  Heftig image"
    if [ "${HEFTIG_BUILD:-0}" = "1" ]; then
        command -v git > /dev/null 2>&1 || die "Building needs git."
        src="$HOME_DIR/source"
        if [ -d "$src/.git" ]; then git -C "$src" pull --ff-only; else git clone --depth 1 "https://github.com/$REPO.git" "$src"; fi
        IMAGE="localhost/heftig:latest"
        $ENGINE build -t "$IMAGE" "$src"
    elif case "$IMAGE" in localhost/*) true ;; *) false ;; esac && $ENGINE image inspect "$IMAGE" > /dev/null 2>&1; then
        say "Using the local image $IMAGE."
    else
        $ENGINE pull "$IMAGE" || die "Could not download $IMAGE. (Offline? Try again, or build it: HEFTIG_BUILD=1)"
    fi

    # --- container -------------------------------------------------------------------------------
    step "4/4  Start"
    if [ "${HEFTIG_LAN:-0}" = "1" ]; then PUBLISH="$PORT:8765"; else PUBLISH="127.0.0.1:$PORT:8765"; fi
    # SELinux (Fedora, RHEL): label the folders for the container
    VOLOPT=""
    if command -v selinuxenabled > /dev/null 2>&1 && selinuxenabled; then VOLOPT=":Z"; fi

    # the arguments as a list (paths may contain spaces); restart policy "always": the container
    # comes back after a crash and after a reboot (Docker's daemon does that itself; rootless
    # Podman needs its podman-restart user service, enabled below)
    set -- run -d --restart always --name "$NAME" -p "$PUBLISH" \
        -v "$HOME_DIR/archive:/archive$VOLOPT" -v "$HOME_DIR/scanner:/consume$VOLOPT" \
        -e "PUID=$(id -u)" -e "PGID=$(id -g)" -e "HEFTIG_HOST_SCANNER_DIR=$HOME_DIR/scanner"
    # a model server on this computer (Ollama ...): Podman names the host host.containers.internal
    # by itself, Docker needs this
    if [ "$ENGINE" = docker ]; then set -- "$@" --add-host host.docker.internal:host-gateway; fi

    old=""
    if $ENGINE container inspect "$NAME" > /dev/null 2>&1; then
        old="$NAME-previous"
        $ENGINE rm -f "$old" > /dev/null 2>&1 || true
        $ENGINE stop "$NAME" > /dev/null 2>&1 || true
        $ENGINE rename "$NAME" "$old" > /dev/null
    fi
    if ! $ENGINE "$@" "$IMAGE" heftig run > /dev/null; then
        if [ -n "$old" ]; then
            $ENGINE rename "$old" "$NAME" > /dev/null 2>&1 && $ENGINE start "$NAME" > /dev/null 2>&1
            die "Starting the new version failed; the previous one runs again."
        fi
        die "Starting Heftig failed (is port $PORT already in use?)."
    fi
    [ -z "$old" ] || $ENGINE rm -f "$old" > /dev/null 2>&1 || true
    if [ "$ENGINE" = podman ] && [ "$(id -u)" != "0" ] && command -v systemctl > /dev/null 2>&1; then
        systemctl --user enable podman-restart.service > /dev/null 2>&1 \
            || say "Note: could not enable podman-restart.service - start Heftig after a reboot with: podman start $NAME"
        # keep running when you are logged out (allowed for your own user on most systems)
        loginctl enable-linger "$(id -un)" > /dev/null 2>&1 \
            || say "Note: Heftig runs while you are logged in (loginctl enable-linger was not allowed)."
    fi

    # --- ready? ----------------------------------------------------------------------------------
    say "Waiting for Heftig to start ..."
    url="http://127.0.0.1:$PORT"
    i=0
    until curl -fs "$url/ready" > /dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -gt 90 ]; then
            $ENGINE logs --tail 30 "$NAME" 2>&1 || true
            die "Heftig did not start. The log above may say why."
        fi
        sleep 1
    done

    setup=""
    if [ -f "$HOME_DIR/archive/setup-token" ]; then
        setup="$url/setup?token=$(cat "$HOME_DIR/archive/setup-token")"
    fi

    step "Heftig is running."
    if [ -n "$setup" ]; then
        say "Open this address to set it up (account, AI, e-mail, scanner):"
        say ""
        say "    $setup"
    else
        say "Open $url"
    fi
    say ""
    if [ "${HEFTIG_LAN:-0}" = "1" ]; then
        if [ "$OS" = Darwin ]; then ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
        else ip=$(hostname -I 2>/dev/null | awk '{print $1}'); fi
        [ -n "$ip" ] || ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
        say "Other devices in your network reach it at http://${ip:-<address of this computer>}:$PORT"
        say "(The phone camera needs HTTPS - see docs/operations.md, \"HTTPS\".)"
    fi
    say "Files:   $HOME_DIR"
    say "Manage:  $ENGINE stop|start $NAME    logs: $ENGINE logs $NAME"
    say "Update:  run the installer again (your data stays)."
}

main "$@"
