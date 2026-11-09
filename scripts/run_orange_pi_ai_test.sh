#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
benchmark="$script_dir/orange_pi_ai_test.py"
model=qwen2.5:1.5b
timeout=180
prompt_args=()
usage='Usage: bash run_orange_pi_ai_test.sh [--model MODEL] [--prompt TEXT] [--timeout SECONDS]'

# Keep the earlier positional model and timeout syntax working.
if [[ ${1-} != -* ]] && (( $# == 1 || $# == 2 )); then
    set -- --model "$1" --timeout "${2-180}"
fi

while (( $# > 0 )); do
    case "$1" in
        -h|--help)
            printf '%s\n' "$usage"
            exit 0
            ;;
        --model|--prompt|--timeout)
            if (( $# < 2 )) || [[ $2 == --* || ! $2 =~ [^[:space:]] ]]; then
                printf 'Provide a value for %s.\n%s\n' "$1" "$usage" >&2
                exit 2
            fi
            case "$1" in
                --model) model=$2 ;;
                --prompt) prompt_args=(--prompt "$2") ;;
                --timeout) timeout=$2 ;;
            esac
            shift 2
            ;;
        *)
            printf '%s\n' "$usage" >&2
            exit 2
            ;;
    esac
done

# Validate overrides before setup can install packages or download a model.
if [[ ! "$model" =~ ^[[:alnum:]] || ! "$timeout" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Provide a model tag and a positive timeout in seconds.\n%s\n' "$usage" >&2
    exit 2
fi

if [[ ! -r "$benchmark" ]]; then
    printf 'Copy orange_pi_ai_test.py into the same directory as this script.\n' >&2
    exit 1
fi

if ! command -v ollama >/dev/null; then
    printf 'Installing Ollama and its prerequisites.\n'
    sudo apt update
    sudo apt install -y python3 curl zstd
    installer=$(mktemp)
    trap 'rm -f -- "$installer"' EXIT
    # Download the whole installer before executing it so a failed transfer stops setup.
    curl -fsSL https://ollama.com/install.sh -o "$installer"
    sh "$installer"
elif ! command -v python3 >/dev/null; then
    sudo apt update
    sudo apt install -y python3
fi

# Keep the CLI on the same local endpoint as the Python benchmark.
export OLLAMA_HOST=127.0.0.1:11434
if ! ollama list >/dev/null 2>&1; then
    if ! sudo systemctl start ollama; then
        printf 'Could not start Ollama. On an image without systemd, run ollama serve in another terminal.\n' >&2
        exit 1
    fi
    # systemctl can return before the Ollama API is ready to accept requests.
    for attempt in {1..10}; do
        if ollama list >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if ! ollama list; then
        printf 'Ollama is still unreachable. Check systemctl status ollama.\n' >&2
        exit 1
    fi
fi

if ! ollama show "$model" >/dev/null 2>&1; then
    ollama pull "$model"
fi

for run in 1 2; do
    printf '\nBenchmark run %s of 2\n' "$run"
    python3 "$benchmark" --model "$model" --timeout "$timeout" "${prompt_args[@]}"
done

printf '\nOllama processor report\n'
ollama ps
