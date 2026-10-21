"""One chat-completion call to DeepSeek or OpenCode Go from a cloud session.

The cloud environment injects the provider's ``Authorization`` header into requests to these hosts, so this script
never sets, reads or prints a key. It sends one request, writes the reply text to ``--out`` and prints a one-line
JSON usage report (``calls`` is 1 per run; ``--count-file`` keeps a running total across runs).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CA_BUNDLE = "/root/.ccr/ca-bundle.crt"
ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/chat/completions",
    "opencode-go": "https://opencode.ai/inference/go/openai/v1/chat/completions",
}


def build_request(provider: str, model: str, prompt: str, max_tokens: int) -> tuple[str, dict, bytes]:
    """URL, headers and body. No Authorization header: the platform adds it."""
    if provider not in ENDPOINTS:
        raise ValueError(f"unknown provider {provider!r}; use one of {sorted(ENDPOINTS)}")
    headers = {"Content-Type": "application/json"}
    if provider == "opencode-go":
        headers["x-opencode-session"] = uuid.uuid4().hex
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    return ENDPOINTS[provider], headers, body


def tls_context(ca_path: str = CA_BUNDLE) -> ssl.SSLContext:
    """Verifying context; the proxy CA bundle is used when present."""
    return ssl.create_default_context(cafile=ca_path if os.path.exists(ca_path) else None)


def main(argv: list[str] | None = None, opener=urllib.request.urlopen) -> int:
    ap = argparse.ArgumentParser(prog="cloud_llm_call", description=__doc__)
    ap.add_argument("--provider", required=True, choices=sorted(ENDPOINTS))
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-tokens", type=int, default=4096,
                    help="reasoning tokens count here; too small leaves the reply empty")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--count-file", default=None, help="running call total, incremented per call made")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read prompt file: {exc}", file=sys.stderr)
        return 1
    url, headers, body = build_request(args.provider, args.model, prompt, args.max_tokens)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener(req, timeout=args.timeout, context=tls_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} from {args.provider}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"request failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if args.count_file:
            cf = Path(args.count_file)
            try:
                n = int(cf.read_text().strip())
            except (OSError, ValueError):
                n = 0
            cf.write_text(str(n + 1), encoding="utf-8")

    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    if not text:
        print("empty reply (finish_reason=%s); raise --max-tokens, reasoning uses the budget"
              % choice.get("finish_reason"), file=sys.stderr)
        return 1
    Path(args.out).write_text(text, encoding="utf-8")
    print(json.dumps({
        "calls": 1,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "finish_reason": choice.get("finish_reason"),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
