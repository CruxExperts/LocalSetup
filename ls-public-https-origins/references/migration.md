# Migration notes

## Native Unix origins

Garage supports Unix socket API bindings. Keep its existing instance, image and
data mounts; move S3 and admin API listeners separately. SFTPGo HTTP bindings
accept a Unix socket address with port zero. Check the deployed versions before
rendering either service's config. Do not convert SMTP, IMAP or Garage RPC to
HTTP; those protocols use their own TLS/security configuration.

Keep the source directory private to its owner and the socket mode 0600.
The optional bridge exposes the same socket inode inside an already mounted
proxy data directory using an atomic hard link. Its target directory must be
root-owned mode 0700, and the proxy workers must have access to that socket.
The included helper assumes root proxy workers; for other worker identities,
design explicit shared-group permissions or use a native protected mount.
Reject cross-filesystem links; do not copy the socket or expose a TCP fallback.
Use trusted, non-symlink ancestor directories writable only by their owning
administrator. The helper does not defend against a malicious privileged owner
changing ancestors during operation.

Render `assets/socket-bridge.*.in`, with absolute paths and trusted unit names,
into systemd service/path units. The source path watcher must run after boot and
republish the new socket after every application restart. ProtectSystem=full
permits linking across existing mounts; separately remounting source and target
with restrictive ReadWritePaths can cause EXDEV even when st_dev matches.
Verify systemd path activation using an authorized actual service restart.

An Nginx upstream can use `proxy_pass http://unix:/data/origin/api.sock:;`.
That URL describes HTTP messages over a protected local Unix socket, with no
TCP HTTP connection. Preserve the public Host and trusted forwarded HTTPS
headers; generate secure cookies. Configure HTTPS public health checks when the
native health command cannot access Unix sockets. Disable or redact access and
error logs that would contain presigned bearer URLs.

## Isolated client egress

Keep the client on an internal backend network without a default Internet
route. Give only the relay required public IPv4 DNS/443 egress. Map the approved
public DNS names to the relay address in the client namespace only; do not
override DNS inside the relay. The client URL, SNI, Host and certificate identity
remain public. The relay rejects non-global upstream DNS addresses, so it needs
the external public answer; deployments needing split DNS to private upstreams
must design an explicit pinned route rather than weakening that guard.

Disable container HTTP proxy inheritance as well as removing application proxy
config. With Podman use `--http-proxy=false`. Inspect the generated service unit;
older Quadlet versions can ignore per-container drop-ins. Render required
`--add-host` arguments into the owned main unit idempotently if necessary.
Do not rewrite unchanged units from ExecStartPre.

The relay bounds handshake size/fragments, active connections, idle time and
connection lifetime. Only the initial visible SNI is inspected. Traffic within
TLS is opaque: shared CDN routing, HTTP Host values and application permissions
must still be enforced at the destination. Every caller must verify TLS.
Clients using encrypted ClientHello without an approved visible name fail
closed. No wildcard SNI matches. S3 virtual-host bucket names each need an exact
allowlist entry; prefer path style when supported. IPv6 upstreams are currently
unsupported. Preserve non-root execution, dropped capabilities, read-only root,
no-new-privileges, resource limits and bounded writable mounts.

## Recovery and evidence

Use the owning backup tool, secret store and terminal workflow; this package
does not replace them. Recover consistent configuration/database snapshots
without losing mail, objects or accounts. Preserve successful evidence for
unchanged layers. Record which services were checked, what restarted, what
failed closed, and any remaining deployment-specific constraints. A syntax
check is not evidence of successful authenticated application access.

Primary references:

- [Garage configuration](https://garagehq.deuxfleurs.fr/documentation/reference-manual/configuration/)
- [SFTPGo configuration](https://docs.sftpgo.com/latest/config-file/)
- [Nginx proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
