import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from scripts import orange_pi_ai_test as benchmark


def test_sends_prompt_and_prints_metrics(monkeypatch, capsys):
    class Ollama:
        def open(self, request, *, timeout):
            assert request.full_url == "http://127.0.0.1:11434/api/generate"
            assert request.get_method() == "POST"
            assert timeout == 90
            payload = json.loads(request.data)
            assert payload["model"] == "test-model"
            assert payload["prompt"] == "Say hello."
            assert payload["stream"] is False
            result = {
                "done": True,
                "done_reason": "length",
                "response": "Hello!",
                "load_duration": 500_000_000,
                "eval_count": 128,
                "eval_duration": 4_000_000_000,
            }
            return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(benchmark, "build_opener", lambda *_: Ollama())
    monkeypatch.setattr(benchmark, "which", lambda _: pytest.fail("A reachable API needs no CLI check"))
    monkeypatch.setattr(
        sys, "argv",
        ["test", "--model", "test-model", "--prompt", "Say hello.", "--timeout", "90"],
    )

    assert benchmark.main() == 0
    output = capsys.readouterr().out
    assert "Hello!" in output
    assert "socket timeout: 90 s" in output
    assert "Total response time" in output
    assert "Model load time: 0.50 s" in output
    assert "32.00 tokens/s" in output
    assert "128-token output limit" in output


@pytest.mark.parametrize(
    ("cli_path", "guidance"),
    [
        (None, "https://docs.ollama.com/linux#install"),
        ("/usr/local/bin/ollama", "systemctl status ollama"),
    ],
)
def test_reports_setup_steps_when_ollama_is_unreachable(monkeypatch, capsys, cli_path, guidance):
    class Ollama:
        def open(self, request, *, timeout):
            raise URLError(ConnectionRefusedError())

    monkeypatch.setattr(benchmark, "build_opener", lambda *_: Ollama())
    monkeypatch.setattr(benchmark, "which", lambda _: cli_path)
    monkeypatch.setattr(sys, "argv", ["test"])

    assert benchmark.main() == 1
    error = capsys.readouterr().err
    assert "Could not reach local Ollama" in error
    assert guidance in error


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (URLError(TimeoutError()), "Ollama timed out"),
        (TimeoutError(), "Ollama timed out"),
        (HTTPError("", 404, "Not Found", {}, None), "ollama pull qwen2.5:1.5b"),
        (HTTPError("", 500, "Internal Server Error", {}, None), "Check the Ollama server logs"),
        (ConnectionResetError(), "connection to Ollama was interrupted"),
        (b"<html>", "invalid JSON"),
        (b'{"done": false}', "completed generation"),
        (b'{"done": true, "response": " "}', "no answer text"),
    ],
)
def test_reports_failure(monkeypatch, capsys, reply, message):
    class Ollama:
        def open(self, request, *, timeout):
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(reply)

    monkeypatch.setattr(benchmark, "build_opener", lambda *_: Ollama())
    monkeypatch.setattr(sys, "argv", ["test"])

    assert benchmark.main() == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["--prompt", "Say hello.", "--timeout", "3000"],
            ["--model", "qwen2.5:1.5b", "--timeout", "3000", "--prompt", "Say hello."],
        ),
        (["--model", "qwen2.5:0.5b"], ["--model", "qwen2.5:0.5b", "--timeout", "180"]),
        (["qwen2.5:0.5b", "300"], ["--model", "qwen2.5:0.5b", "--timeout", "300"]),
        (["--prompt", "--timeout", "300"], None),
    ],
)
def test_launcher_forwards_options_to_both_runs(args, expected):
    bash = shutil.which("bash")
    if bash is None or (sys.platform == "win32" and Path(bash).parent.name.lower() == "system32"):
        pytest.skip("Requires Bash that can run local files directly")

    launcher = Path(benchmark.__file__).with_name("run_orange_pi_ai_test.sh")
    # Stub external commands so the test never installs packages or sends prompts.
    harness = '''
ollama() {
    if [[ "$1" == show ]]; then return 1; fi
    printf 'ollama %s\\n' "$*"
}
python3() { shift; printf 'arg=%s\\n' "$@"; }
sudo() { return 99; }
export -f ollama python3 sudo
exec "$BASH" "$@"
'''
    result = subprocess.run(
        [bash, "-c", harness, "test", launcher.as_posix(), *args],
        capture_output=True, text=True, timeout=10,
    )

    if expected is None:
        assert result.returncode == 2
        assert "Provide a value for --prompt" in result.stderr
        assert not result.stdout
    else:
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert f"ollama pull {expected[1]}" in lines
        assert [line[4:] for line in lines if line.startswith("arg=")] == expected * 2
