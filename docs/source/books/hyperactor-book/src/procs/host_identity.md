# Host identity discovery over duplex

A Host frontend address identifies an endpoint. Its concrete service `ProcId`,
such as `service<gVQHWd9h6Km>`, identifies one Host incarnation. A default Host
keeps that ID throughout its lifetime and allocates a fresh ID when restarted
at the same endpoint. Retained actor references continue to name the old
incarnation, even if its replacement creates actors with the same labels.

Clients knowing only the frontend discover its service proc on a private duplex
session:

```rust,ignore
use hyperactor_mesh::host::query_service_proc;

let service = query_service_proc(frontend).await?;
```

The resulting `ProcAddr` contains the concrete instance ID and normalized dial
location. Discovery registers no Gateway peer and changes no caller route.
A shared `service` singleton cannot distinguish physical Hosts or successive
incarnations. Discovering the ID lets the Host allocate it at startup without
requiring launchers to distribute it separately.

## One-shot protocol

The Host supplies its immutable service ID to its frontend before listening.
The advertisement belongs to that listener; other procs with the same label do
not affect the answer. The first message selects one operation for that duplex connection:

| First message | Behavior |
| --- | --- |
| `IdentifyService` | Reply once with the advertised concrete ID (or an explicit unavailable/malformed-request error), then close. No peer is registered. |
| `AttachRequest` | Existing Gateway attachment handshake and message serving. |
| `AttachAndIdentify { uid }` | Validate the advertised identity, register the peer, and reply with both its via location and service ID; then serve actor messages. Unavailable identity or duplicate peer registration rejects the attachment. |
| Ordinary actor envelope | Existing inbound routing, including delivery of that first envelope. |

The identity response is appended to `AttachWire`, preserving the existing wire
variant ordering. Classification uses the request's type hash, so ordinary actor
payloads need no extra deserialization. No continuing control-message inspection
is added to ordinary messaging.

The querying client sends one request and expects one identity reply. Unexpected
replies, including actor envelopes, are errors: this session has no peer route
through which legitimate actor messages could arrive. Concrete instance IDs are
required; singleton IDs are rejected both when advertised and when received.

The query has a 30-second deadline. Its private client is cancelled on error,
timeout, or caller cancellation, and joined after receiving a reply. The server
waits for reply delivery before closing, bounded by the same deadline and listener
shutdown. Disconnected duplex sessions expire after the configured message-delivery
timeout so abandoned replies cannot retain sessions forever. Connected, idle
attachments retain their existing behavior. Completed connection tasks are reaped;
failed joins are logged both during normal serving and shutdown.

## Optional combined attachment

A caller that needs both discovery and Gateway attachment can avoid redialing:

```rust,ignore
let (attachment, service_proc) = gateway.serve_via_and_identify(frontend).await?;
```

This sends a single `AttachAndIdentify { uid }` request and expects one
`AttachedWithIdentity { location, service_proc }` reply. Failures use the existing
`Ack(Rejected)` response. An unavailable identity is rejected before registering
any peer; rejecting a duplicate uid leaves its existing attachment untouched.
The new success variant is appended after existing `AttachWire` variants. Use
separate query and attach operations with frontends that do not support the
combined request.

The combined operation shares the [attachment lifecycle](gateway_connection.md)
with `serve_via`: serialize attachment attempts, retain interleaved inbound actor
messages, drain the previous outbound route, then publish the new route. A failed
or cancelled attempt releases queued sends through the current live fallback.
There is no arbitrary control plan or repeated-query setup loop. Actor traffic
may precede the reply because peer registration precedes acceptance, so the shared
attachment buffer still applies.

Standalone `IdentifyService` remains one-shot and registers no peer. Callers may
still query first and attach on another connection. Both forms learn a snapshot
of the Host incarnation; neither silently redirects stale IDs after a restart.

An ordinary duplex client may remain connected indefinitely before sending its
first message. The server cannot classify that connection as a control exchange
yet, so it does not impose a setup timeout on the first-message wait. Client
control deadlines and bounded rejection delivery do not limit ordinary idle
connections or successfully attached sessions.

This combined operation is an optional feature on top of the independent
discovery and attachment fixes; neither fix requires it.

## Supplied IDs and Python bootstrap

`attach_to_workers(workers=["tcp://host:4444"], ...)` discovers the current service
incarnation before contacting its HostAgent. An explicit concrete
`service<uid>@channel-url` bypasses discovery, including for non-duplex transports.
Callers may still supply their own concrete IDs; reusing an ID across restarts
remains their responsibility. Slurm and Kubernetes identity allocation are unchanged.

Tests cover frontend-specific and concurrent queries, unchanged ordinary traffic,
identify followed by a separate attachment, invalid/unavailable replies, cancellation,
timeouts, session cleanup, and listener shutdown. Host and Python restart tests
confirm that a replacement is reachable under its new ID while stale references
fail. Combined-attachment tests run the same saved-forwarder, drain, failure, and
cancellation regressions through both public APIs, and check malformed requests,
unavailable identities, duplicate peers, invalid replies, and first-message idleness.
