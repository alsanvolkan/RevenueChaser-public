"""Tests for scripts.cloud_llm_call: offline, the HTTP layer is faked."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from RevenueChaser.scripts import cloud_llm_call as c

_OK = {
    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 20,
              "completion_tokens_details": {"reasoning_tokens": 15}},
}


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener(payload=None, seen=None):
    def op(req, timeout=None, context=None):
        if seen is not None:
            seen.append(req)
        return _Resp(json.dumps(payload or _OK).encode())
    return op


def test_deepseek_request_shape():
    url, headers, body = c.build_request("deepseek", "deepseek-flash", "p", 64)
    assert url == "https://api.deepseek.com/chat/completions"
    assert json.loads(body) == {"model": "deepseek-flash", "max_tokens": 64,
                                "messages": [{"role": "user", "content": "p"}]}
    assert "x-opencode-session" not in {k.lower() for k in headers}


def test_opencode_request_has_session_header():
    url, headers, _ = c.build_request("opencode-go", "deepseek-v4-flash", "p", 64)
    assert url == "https://opencode.ai/inference/go/openai/v1/chat/completions"
    assert {k.lower() for k in headers}.issuperset({"x-opencode-session", "content-type"})


@pytest.mark.parametrize("prov", ["deepseek", "opencode-go"])
def test_never_adds_authorization(prov):
    _, headers, _ = c.build_request(prov, "m", "p", 8)
    assert "authorization" not in {k.lower() for k in headers}


def test_bad_provider_rejected():
    with pytest.raises(ValueError):
        c.build_request("openai", "m", "p", 8)


def test_main_writes_out_and_reports_usage(tmp_path, capsys):
    pf, out = tmp_path / "p.txt", tmp_path / "o.txt"
    pf.write_text("question", encoding="utf-8")
    seen: list = []
    rc = c.main(["--provider", "deepseek", "--model", "deepseek-flash", "--prompt-file", str(pf),
                 "--out", str(out), "--max-tokens", "50"], opener=_opener(seen=seen))
    assert rc == 0 and out.read_text(encoding="utf-8") == "hi"
    assert len(seen) == 1 and "authorization" not in {k.lower() for k in seen[0].headers}
    rep = json.loads(capsys.readouterr().out)
    assert rep == {"calls": 1, "prompt_tokens": 7, "completion_tokens": 20,
                   "reasoning_tokens": 15, "finish_reason": "stop"}


def test_empty_content_with_length_is_failure(tmp_path, capsys):
    pf = tmp_path / "p.txt"
    pf.write_text("q", encoding="utf-8")
    bad = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}}
    rc = c.main(["--provider", "deepseek", "--model", "m", "--prompt-file", str(pf),
                 "--out", str(tmp_path / "o")], opener=_opener(bad))
    assert rc == 1 and "max-tokens" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_http_error_returns_1_without_body_leak(tmp_path, capsys):
    pf = tmp_path / "p.txt"
    pf.write_text("q", encoding="utf-8")

    def op(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"nope"))
    rc = c.main(["--provider", "deepseek", "--model", "m", "--prompt-file", str(pf),
                 "--out", str(tmp_path / "o")], opener=op)
    assert rc == 1 and "HTTP 401" in capsys.readouterr().err


def test_missing_prompt_file(tmp_path):
    assert c.main(["--provider", "deepseek", "--model", "m", "--prompt-file", str(tmp_path / "x"),
                   "--out", str(tmp_path / "o")], opener=_opener()) == 1


def test_count_file_accumulates(tmp_path):
    pf, cf = tmp_path / "p.txt", tmp_path / "n"
    pf.write_text("q", encoding="utf-8")
    for _ in range(2):
        c.main(["--provider", "deepseek", "--model", "m", "--prompt-file", str(pf),
                "--out", str(tmp_path / "o"), "--count-file", str(cf)], opener=_opener())
    assert cf.read_text().strip() == "2"


def test_tls_context_verifies(monkeypatch, tmp_path):
    import ssl
    ctx = c.tls_context(ca_path=str(tmp_path / "missing.crt"))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
