"""Send one prompt to local Ollama and report the response and timings."""

import argparse
import json
import sys
from http.client import HTTPException
from shutil import which
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_MODEL = "qwen2.5:1.5b"
DEFAULT_PROMPT = "In two sentences, explain what DNS does and give one example."
GENERATE_URL = "http://127.0.0.1:11434/api/generate"
INSTALL_URL = "https://docs.ollama.com/linux#install"
MAX_OUTPUT_TOKENS = 128


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help="Ollama model tag (default: %(default)s)"
    )
    parser.add_argument(
        "--prompt", default=DEFAULT_PROMPT, help="Prompt text (default: %(default)s)"
    )
    parser.add_argument(
        "--timeout", type=int, default=180,
        help="Socket timeout in seconds (default: %(default)s)",
    )
    args = parser.parse_args()
    if not args.model.strip() or not args.prompt.strip() or args.timeout <= 0:
        parser.error("Provide a non-empty model and prompt, and a positive timeout.")

    # Bound context memory and generation work for a short test on the Pi.
    payload = {
        "model": args.model,
        "prompt": args.prompt,
        "stream": False,
        "options": {"num_ctx": 2048, "num_predict": MAX_OUTPUT_TOKENS, "temperature": 0},
    }
    request = Request(
        GENERATE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # Bypass system proxies so the prompt goes directly to the local server.
    opener = build_opener(ProxyHandler({}))
    print(
        f"Model: {args.model}\nWaiting for local Ollama (socket timeout: {args.timeout} s)...",
        flush=True,
    )
    started = perf_counter()
    try:
        with opener.open(request, timeout=args.timeout) as response:
            body = response.read()
    except HTTPError as exc:
        exc.close()
        if exc.code == 404:
            print(
                "Ollama returned HTTP 404. Check the installed models with 'ollama list'. "
                f"If the model is missing, run 'ollama pull {args.model}' and retry.",
                file=sys.stderr,
            )
        else:
            print(f"Ollama returned HTTP {exc.code}. Check the Ollama server logs.", file=sys.stderr)
        return 1
    except (TimeoutError, URLError) as exc:
        if isinstance(exc, TimeoutError) or isinstance(exc.reason, TimeoutError):
            print("Ollama timed out. Try a smaller model or raise --timeout.", file=sys.stderr)
        # A running service can work without the CLI on PATH, so check only after a failure.
        elif which("ollama") is None:
            print(
                "Could not reach local Ollama, and 'ollama' is missing from PATH. "
                f"Install Ollama or check PATH, then verify the service is running. {INSTALL_URL}",
                file=sys.stderr,
            )
        else:
            print(
                "Could not reach local Ollama at 127.0.0.1:11434. "
                "On Ubuntu, check 'systemctl status ollama'. "
                "For a manual start, run 'ollama serve' in another terminal.",
                file=sys.stderr,
            )
        return 1
    except (OSError, HTTPException):
        print("The connection to Ollama was interrupted. Check the Ollama server logs.", file=sys.stderr)
        return 1
    elapsed = perf_counter() - started

    try:
        result = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("Ollama returned invalid JSON.", file=sys.stderr)
        return 1
    if not isinstance(result, dict) or result.get("done") is not True:
        print("Ollama did not return a completed generation.", file=sys.stderr)
        return 1
    answer = result.get("response")
    if not isinstance(answer, str) or not answer.strip():
        print("Ollama returned no answer text. Try a smaller instruction model.", file=sys.stderr)
        return 1

    print(f"\nResponse\n{answer.strip()}\n\nTotal response time: {elapsed:.2f} s")
    load_ns = result.get("load_duration")
    if isinstance(load_ns, int) and load_ns >= 0:
        print(f"Model load time: {load_ns / 1e9:.2f} s")
    tokens = result.get("eval_count")
    generation_ns = result.get("eval_duration")
    # Ollama reports nanoseconds. Use generation time to exclude loading and prompt evaluation.
    if isinstance(tokens, int) and isinstance(generation_ns, int) and generation_ns > 0:
        print(f"Generation: {tokens} tokens at {tokens / (generation_ns / 1e9):.2f} tokens/s")
    if result.get("done_reason") == "length":
        print(f"The model reached the {MAX_OUTPUT_TOKENS}-token output limit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
