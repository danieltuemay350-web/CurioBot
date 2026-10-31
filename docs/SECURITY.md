# Security and Trust Boundaries

**Status:** The application-level safeguards below are implemented in this source tree and covered by local tests where possible. This does not claim live Telegram/Gemini validation or deployment-level egress enforcement.

## Assets and trust boundaries

Assets include Telegram/Gemini credentials, the administrator allowlist, target channel access, SQLite records, draft and approval history, and operational logs. Trust boundaries are crossed when the system receives Telegram input, reads RSS feeds, fetches webpages, calls Gemini, and sends messages to Telegram.

Assume that submitted URLs, feed URLs and entries, page content, redirects, extracted text, AI-discovered sources, LLM output, and network responses may be malicious or malformed. Only an authenticated, authorized administrator may approve sources/drafts or control operation. Model output is never an authority boundary.

## SSRF and safe fetching

URL validation is necessary but alone does not completely prevent server-side request forgery (SSRF). Fetching must use a dedicated safe-fetch path for both feed and webpage requests.

- Allow only `http` and `https`; reject unsupported schemes, embedded URL credentials, malformed hostnames, and disallowed ports according to deployment policy.
- Resolve destination addresses and reject loopback, private, link-local, multicast, unspecified, IPv4-mapped private IPv6, and reserved IP ranges for IPv4 and IPv6. Do not rely on `ipaddress.is_global` alone because Python classifies multicast addresses as global. Fail closed when resolution or classification is uncertain.
- Recheck every redirect, resolve its destination, and validate the actual connected address. Do not let an HTTP client transparently follow redirects without applying the policy at each hop.
- Address DNS rebinding and time-of-check/time-of-use gaps by binding the TCP connection to a validated address and using the hostname for TLS SNI/certificate verification. Use a constrained outbound proxy/network policy as defense in depth where available; no VPS firewall policy is included in this local MVP.
- Set strict connection and read timeouts, redirect-count limits, response-byte limits, and content-type allowlists. Reject oversized, unsupported, or malformed responses before parsing. Limit decompression and parser resource use as well.
- Do not forward arbitrary response headers, local paths, credentials, or internal error details to Telegram or logs.
- Apply the same checks to configured RSS feeds, discovered candidates, and administrator-submitted URLs; administrative intent does not make a destination safe.

## Prompt injection and AI boundaries

Fetched content and metadata are data, not instructions. Keep system/developer instructions structurally separate from article text and explicitly label source material as untrusted. Use no model tools, credentials, or direct publishing/source-activation capabilities. Validate structured output against a schema and treat it as untrusted even when valid. The application—not the model—enforces source lifecycle, approval, budget, and publishing state.

Do not send sensitive or private content to Gemini. Use only the configured model and official SDK. Stop generation when the daily budget is exhausted or quota availability is uncertain. Never silently switch provider/model or fall back to a paid model.

## Identity, secrets, and authorization

- Keep Telegram and Gemini credentials in environment-based secret configuration; never commit them, print them, or expose them in user-facing errors.
- Authorize every management command and callback using the administrator Telegram user ID allowlist. Default deny, including for group/channel contexts unless explicitly supported.
- Verify callback targets and state transitions server-side; hiding a button is not authorization.
- Avoid logging full source text, sensitive submissions, credentials, or raw provider/network errors. Sanitize and bound error details.
- Protect the SQLite file and backups with restrictive filesystem permissions. Keep backups outside the running database directory and protect them as production data.

## Source and publication integrity

AI-discovered sources remain quarantined until independent validation and explicit administrator approval. A model cannot approve or activate sources. Generated posts require administrator approval in the MVP. Approval and source lifecycle transitions must be audited transactionally.

Persist a publication attempt before sending. If Telegram's result is ambiguous, record `unknown`, notify the administrator, and require manual reconciliation. Do not blindly retry. Exactly-once delivery cannot be guaranteed across network/process failure. A confirmed publication needs the Telegram message ID and success timestamp; unknown attempts must not be represented as confirmed publications.

## Copyright, attribution, and retention

Create concise original explanations rather than reproducing article passages or closely imitating wording. Include source name and canonical URL; include author/date when available. Check factual claims against source material and flag unsupported claims for human review. Similarity checks can flag risk but do not establish legal compliance.

Extracted article text expires after seven days by default, with a configurable environment setting. Keep expiring text separate from permanent metadata, drafts, publication history, and audit records. Do not retain source text indefinitely merely to simplify regeneration. Permanent records are retained indefinitely by default pending an explicit future retention policy.

## Threats not eliminated by this design

Controls reduce risk but do not make hostile web content, DNS, model output, copyright questions, or network delivery perfectly safe. SSRF protection depends on actual connection behavior and deployment egress configuration; prompt defenses cannot guarantee that generated content is correct; human approval remains important. Document implementation evidence and security review findings rather than treating this document as proof of controls.

The fetcher uses a pinned standard-library HTTP connection rather than allowing a generic client to re-resolve a validated hostname during connection. Transport tests exercise the real `http.client` framing and pinned-IP argument through a controlled socket seam, with DNS answers and responses fully local to the test. They do not complete a live TCP connection or TLS handshake: the restricted environment denies binding a local server, so end-to-end socket routing and certificate verification still require verification in an isolated environment that permits local networking.
