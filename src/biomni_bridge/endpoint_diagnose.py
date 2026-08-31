from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

import requests

from .config import ConfigError, Settings, scrub_secret_environment

_LONG_TASK = "Write a detailed essay of at least 2000 words about mitochondria."


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _completion_tokens(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("completion_tokens")
    return value if isinstance(value, int) else None


def _post(
    settings: Settings,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> tuple[dict[str, Any] | None, float, str]:
    started = time.monotonic()
    try:
        response = requests.post(
            f"{settings.base_url}/chat/completions",
            headers=_headers(settings.api_key),
            json=payload,
            timeout=timeout,
        )
    except requests.Timeout:
        return None, time.monotonic() - started, f"timed out after {timeout:g}s"
    except requests.RequestException as exc:
        return None, time.monotonic() - started, type(exc).__name__

    seconds = time.monotonic() - started
    if not response.ok:
        return None, seconds, f"HTTP {response.status_code}"
    try:
        data = response.json()
    except ValueError:
        return None, seconds, "HTTP 200 with non-JSON response"
    if not isinstance(data, dict):
        return None, seconds, "HTTP 200 with unexpected JSON response"
    return data, seconds, ""


def _probe_cap(
    settings: Settings,
    model: str,
    field: str,
    *,
    cap: int,
    timeout: float,
    findings: list[str],
) -> bool:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _LONG_TASK}],
        "temperature": 0.0,
        "stream": False,
        field: cap,
    }
    data, seconds, error = _post(settings, payload, timeout=timeout)
    if error:
        print(f"  {field:24s} FAILED after {seconds:.1f}s  {error}")
        findings.append(f"{field} probe failed: {error}")
        return False

    used = _completion_tokens(data)
    if used is None:
        print(f"  {field:24s} no usage reported, cannot tell ({seconds:.1f}s)")
    elif used <= cap + 5:
        print(f"  {field:24s} HONOURED   {used} tokens in {seconds:.1f}s")
    else:
        print(f"  {field:24s} DROPPED    {used} tokens in {seconds:.1f}s")
        findings.append(f"{field} appears to be ignored by this endpoint; output may be effectively unbounded.")
    return True


def _probe_thinking(
    settings: Settings,
    model: str,
    *,
    timeout: float,
    findings: list[str],
) -> bool:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "What is 2+2? Answer with the number only."}],
        "temperature": 0.0,
        "stream": False,
        "max_tokens": 2048,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data, seconds, error = _post(settings, payload, timeout=timeout)
    if error:
        print(f"  chat_template_kwargs     REJECTED  {error}")
        findings.append("chat_template_kwargs is rejected; disable BIOMNI_QWEN_DISABLE_THINKING for this provider.")
        return False

    used = _completion_tokens(data)
    print(f"  chat_template_kwargs     accepted, {used if used is not None else '?'} tokens in {seconds:.1f}s")
    if used is not None and used > 200:
        findings.append(f"Thinking may still be active: the trivial probe used {used} completion tokens.")
    return True


def _probe_throughput(
    settings: Settings,
    model: str,
    *,
    timeout: float,
    completion_cap: int,
) -> float | None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _LONG_TASK}],
        "temperature": 0.0,
        "stream": False,
        "max_tokens": completion_cap,
    }
    if "qwen3" in model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    data, seconds, error = _post(settings, payload, timeout=timeout)
    if error:
        print(f"  throughput               FAILED  {error}")
        return None
    used = _completion_tokens(data) or 0
    if not used or seconds <= 0:
        print("  throughput               could not measure")
        return None
    rate = used / seconds
    print(f"  throughput               {rate:.1f} tokens/sec ({used} in {seconds:.1f}s)")
    return rate


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe output caps, Qwen thinking controls, and non-streaming generation speed."
    )
    parser.add_argument("--model", help="Model ID. Defaults to BIOMNI_MODEL/BIOMNI_LLM.")
    parser.add_argument("--timeout", type=float, default=300.0, help="Per-request timeout in seconds (default: 300).")
    parser.add_argument("--cap", type=int, default=32, help="Small output-cap probe size (default: 32 tokens).")
    parser.add_argument(
        "--throughput-tokens",
        type=int,
        default=512,
        help="Completion size for the throughput probe (default: 512 tokens).",
    )
    parser.add_argument(
        "--proxy-idle-timeout",
        type=float,
        default=float(os.getenv("BIOMNI_EXPECTED_PROXY_IDLE_TIMEOUT", "60")),
        help="Expected proxy idle/read timeout in seconds (default: 60).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.timeout <= 0 or args.cap <= 0 or args.throughput_tokens <= 0 or args.proxy_idle_timeout <= 0:
        print("All timeout/token arguments must be greater than zero.", file=sys.stderr)
        return 2

    try:
        settings = Settings.from_env()
        settings.require_credentials()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    scrub_secret_environment()

    model = (args.model or settings.default_model or "").strip()
    if not model:
        print("Set BIOMNI_MODEL or pass --model.", file=sys.stderr)
        return 2

    findings: list[str] = []
    print(f"Endpoint : {settings.base_url}")
    print(f"Model    : {model}")
    print(f"Assumed proxy idle/read timeout: {args.proxy_idle_timeout:g}s\n")

    probes_ok = True
    print("Output caps")
    probes_ok &= _probe_cap(settings, model, "max_tokens", cap=args.cap, timeout=args.timeout, findings=findings)
    probes_ok &= _probe_cap(
        settings, model, "max_completion_tokens", cap=args.cap, timeout=args.timeout, findings=findings
    )

    print("\nQwen/vLLM optional parameter")
    if "qwen3" in model.lower():
        probes_ok &= _probe_thinking(settings, model, timeout=args.timeout, findings=findings)
    else:
        print("  chat_template_kwargs     skipped (model ID is not Qwen3)")

    print("\nNon-streaming generation speed")
    rate = _probe_throughput(
        settings,
        model,
        timeout=args.timeout,
        completion_cap=args.throughput_tokens,
    )
    if rate is None:
        probes_ok = False

    print("\n" + "=" * 72)
    if rate:
        print("Estimated non-streaming completion times at the measured rate:")
        exceeds_timeout = False
        for cap in (256, 512, 1024, 2048, 4096, 8192):
            seconds = cap / rate
            marker = "  <-- exceeds proxy idle timeout" if seconds >= args.proxy_idle_timeout else ""
            exceeds_timeout = exceeds_timeout or bool(marker)
            print(f"  {cap:5d} tokens: {seconds:7.0f}s{marker}")

        measured_seconds = args.throughput_tokens / rate
        if measured_seconds >= args.proxy_idle_timeout * 0.75:
            findings.append(
                f"Non-streaming generation is close to the proxy idle timeout: "
                f"{args.throughput_tokens} tokens take about {measured_seconds:.0f}s versus "
                f"{args.proxy_idle_timeout:g}s. Use streaming transport and/or raise proxy_read_timeout."
            )
        elif exceeds_timeout:
            findings.append(
                "Long non-streaming completions can exceed the assumed proxy idle timeout. "
                "Streaming transport avoids long periods with no upstream response bytes."
            )

    if findings:
        print("\nWhat to act on:")
        for item in findings:
            print(f"  - {item}")
    else:
        print("\nNo anomaly detected by these probes.")
    return 0 if probes_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
