# Model server and proxy tuning for Biomni Bridge

This note is for operators of the OpenAI-compatible endpoint used by Biomni Bridge.

## Client defaults

Biomni Bridge defaults to:

```text
BIOMNI_LLM_STREAM_TRANSPORT=true
BIOMNI_QWEN_DISABLE_THINKING=true
```

Streamed transport keeps long HTTP responses active while the bridge aggregates chunks back into the normal message object Biomni expects.

For model IDs containing `qwen3`, the bridge can send this request-scoped vLLM option:

```json
{
  "chat_template_kwargs": {
    "enable_thinking": false
  }
}
```

This avoids changing model-server behavior for other clients.

## nginx example

For an nginx location proxying the OpenAI-compatible API to a model backend:

```nginx
location /v1/ {
    proxy_pass http://model_backend/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    proxy_buffering off;
    proxy_cache off;

    proxy_read_timeout 1200s;
    proxy_send_timeout 1200s;
    client_max_body_size 8m;
}
```

Adapt `proxy_pass`, path rewriting, timeouts, and body limits to your own deployment rather than copying them blindly.

### Why these settings matter

- `proxy_read_timeout 1200s`: allows long model generations; streamed chunks reset the upstream-read idle interval.
- `proxy_send_timeout 1200s`: gives a busy backend enough time to receive a large request.
- `proxy_buffering off`: lets SSE/streaming chunks pass through promptly.
- `proxy_http_version 1.1`: supports a normal long-lived upstream HTTP connection.
- `client_max_body_size 8m`: leaves headroom for large Biomni prompts while retaining a finite request limit.

Check every proxy/load-balancer layer. Increasing the innermost nginx timeout does not help if an outer gateway closes idle connections earlier.

## vLLM tuning

Do not assume a faster GPU automatically means lower interactive latency. Autoregressive decode can be dominated by model architecture, precision, tensor/pipeline communication, scheduling, and interconnect topology.

Useful measurements include:

- queue time;
- prompt token count;
- time to first token (TTFT);
- output token count;
- inter-token latency;
- generation tokens/second;
- KV-cache utilization;
- GPU memory utilization;
- worker restarts/OOM events.

When comparing configurations, keep the model checkpoint, precision, prompt, output cap, and concurrency constant.

### Parallelism

Benchmark tensor parallelism rather than assuming more shards are faster for one request. A model that fits on fewer GPUs may have lower decode latency because it performs less cross-GPU communication.

Pipeline parallelism adds another communication/scheduling dimension and should normally be introduced because memory/topology requires it, not merely because more GPUs are available.

For mixture-of-experts models, expert parallelism may be relevant; benchmark it separately from dense-model configurations.

### Scheduler controls

Depending on the vLLM version/model, useful controls can include:

- `--max-num-batched-tokens`;
- `--max-num-seqs`;
- chunked prefill;
- prefix caching;
- KV-cache precision;
- speculative decoding/MTP where the model supports it.

Tune from real Biomni request traces rather than tiny chat prompts alone.

## Qwen thinking

If the endpoint does not accept `chat_template_kwargs.enable_thinking`, disable the bridge-side option:

```bash
export BIOMNI_QWEN_DISABLE_THINKING=false
```

If a dedicated vLLM instance should disable thinking for every client, vLLM can also be configured at the server level. Request-level control is safer when the endpoint is shared with other applications.

## Diagnostics from Biomni Bridge

With environment credentials configured:

```bash
make endpoint-check
make endpoint-diagnose
```

or:

```bash
biomni-bridge-endpoint-check --model YOUR_MODEL --timeout 120

biomni-bridge-endpoint-diagnose \
  --model YOUR_MODEL \
  --timeout 300 \
  --proxy-idle-timeout 1200
```

For a failing real request, temporarily enable:

```bash
export BIOMNI_DEBUG_LLM_REQUESTS=true
```

Then compare the captured request metadata with proxy/model-server timestamps. Captures contain the real user prompt and should not be committed or shared casually.
