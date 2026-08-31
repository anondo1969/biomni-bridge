# Security

Read this before allowing anyone other than yourself to reach a running Biomni Bridge instance.

## The one thing that matters

**Biomni writes code and executes it.** That is a core part of what makes the agent useful. It also means a running instance must be treated much more like a remote code-execution environment than a conventional chat application.

Code executed by the agent may be able to:

- read files available inside the container, including mounted data and generated outputs;
- inspect process/container state that is readable by the application user;
- make outbound network connections wherever the container can reach;
- start subprocesses or background activity that can outlive a normal request;
- consume CPU, memory, disk, and network resources available to the container.

Upstream Biomni warns that generated code runs with system privileges available to the Biomni process and recommends isolated/sandboxed environments for production use.

**The correct mental model is: anyone who can submit arbitrary tasks to the same Biomni execution environment must be trusted roughly as much as someone who can run code in that environment.**

## Public deployment

A single Biomni Bridge process is **not suitable as an open public multi-tenant service**.

The UI has no built-in user authentication boundary, and queued Gradio sessions do not create operating-system or process isolation. Session separation prevents normal application state and configuration from being mixed accidentally; it does not protect mutually hostile users from one another.

If several people need access:

- prefer one process/container per trusted user or per trust boundary;
- put real authentication and access control in front of each instance;
- restrict outbound network access where possible;
- mount only the data that instance is allowed to read;
- use scoped, revocable model API credentials;
- use a stronger sandbox if generated code must be treated as untrusted.

Do not interpret Docker, a Gradio queue, or a password field as a sandbox.

## Credentials

Biomni Bridge makes a deliberate effort to reduce accidental credential exposure, but there is an important limit.

### What the bridge does

- Environment credentials are loaded into `Settings`, then wrapper API-key variables are removed from `os.environ` so generated subprocesses do not automatically inherit an easy environment copy.
- UI-entered keys are held in that browser session's server-side runtime and are not stored in browser local storage by the bridge.
- The password textbox is cleared after a successful connection.
- API keys are excluded from `Settings.__repr__` and from normal diagnostic metadata.
- Request-capture diagnostics recursively remove known authentication/key fields and redact the configured secret if it appears in a captured string.
- Each browser session has its own `Settings`, adapter, model selection, and output subdirectory.
- Normal Biomni graph steps are serialized around one process-wide configuration lock.
- Immediately before an active graph step, the bridge reapplies that session's model/base URL and temporarily places that session's API key into Biomni's process-global `default_config.api_key` because some Biomni 0.0.8 database/retrieval helpers create their own LLM clients from `default_config`.
- The key is cleared from `default_config.api_key` in `finally` before the bridge releases the configuration lock.
- A regression test exercises the critical two-session connect/run/disconnect race: `tests/test_session_global_config_isolation.py`.

### What the bridge cannot guarantee

While a user's Biomni graph/tool step is active, that user's key is necessarily available to the Python process because Biomni needs it for model calls. Code executing in the same process cannot be cryptographically isolated from that memory.

A hostile task could also attempt to leave a thread or subprocess behind and observe later process/container state. The wrapper's global lock prevents **normal queued sessions** from accidentally borrowing another session's endpoint/key; it does not turn one Python process into a hostile multi-tenant secret boundary.

Therefore:

- do not share one instance between mutually untrusted users;
- do not use high-value administrative credentials in a shared instance;
- prefer a separate container/process per user when credentials must be strongly isolated.

## Mounted files and outputs

Mount only paths that you are willing for generated code to read.

The documented Docker command mounts only:

- `/data` for Biomni reference/input data;
- `/output` for generated artifacts.

Do **not** mount the Docker socket, SSH keys, cloud credentials, your home directory, or unrelated sensitive project directories.

Use read-only mounts (`:ro`) where the workflow permits it. A read-only mount reduces accidental modification, but it does not make the contents confidential from code running inside the container.

Biomni's data lake also contains resources with their own license/usage terms. Treat licensing separately from confidentiality.

## Endpoint URLs and SSRF

When a user enters an endpoint in the UI, the **server process** makes the network request. A freely chosen URL can therefore borrow the container's network position and become a server-side request-forgery (SSRF) primitive.

By default:

```text
BIOMNI_UI_PUBLIC_ENDPOINTS_ONLY=true
```

UI-entered endpoints must:

- use `https://`;
- contain no embedded username/password;
- not resolve to loopback, private, link-local, reserved, or other non-global addresses.

The validator evaluates literal IP addresses and all DNS results returned for a hostname. Model discovery also disables HTTP redirects so a validated endpoint cannot simply redirect `GET /models` elsewhere.

For an even narrower policy, configure an exact host allowlist:

```text
BIOMNI_ALLOWED_API_HOSTS=models.example.org,other.example.org
```

Environment-configured endpoints are intended for trusted local/self-hosted use and may point at private services.

This URL validation protects the **endpoint field** only. It does not restrict arbitrary network access performed by Biomni-generated code. Use container/network policy for that.

## Request diagnostics

`BIOMNI_DEBUG_LLM_REQUESTS=true` writes a sanitized provider-facing request to the output directory.

Authentication fields are removed, but the capture contains the real task and prompt. Do not enable it casually on shared machines, and never commit captures to source control.

## Dependency and upstream security

Biomni Bridge pins `biomni==0.0.8` and a compatibility-sensitive Python 3.11 dependency stack. The default runtime intentionally does not install the historical optional FastMCP/ToolUniverse stack used by some Biomni environments.

Keep dependencies updated deliberately, review upstream Biomni security guidance, and re-run the complete compatibility/security tests after any Biomni or major dependency upgrade.

## Reporting a problem

For a vulnerability or bug in Biomni Bridge, open an issue in this repository without including real API keys, credentials, private prompts, or sensitive data.

For an issue in Biomni itself, report it to the upstream project:

<https://github.com/snap-stanford/Biomni>
