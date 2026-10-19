# Migration notes

## Native Unix origins

Garage 2.4.1 source accepts absolute Unix paths for HTTP API bindings. Keep its
existing instance, image and data mounts; move S3 and admin API listeners
separately. Garage RPC remains TCP. SFTPGo's `httpd` binding address accepts a
Unix socket path on Unix-like systems; inspect its separate telemetry binding
too. Check the deployed versions before rendering either service's config. Do
not convert SMTP or IMAP to HTTP; those protocols use their own TLS/security
configuration.

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
into systemd service/path units. `PathChanged=` requests activation when the
path changes; it does not guarantee publication of an existing socket at boot
or restart a bridge service that is already running. Enable the path unit at
boot and verify initial publication and actual socket recreation after an
application restart. `PrivateNetwork=` permits filesystem pathname Unix sockets,
but not abstract sockets; filesystem namespace and permissions still matter.
ProtectSystem=full
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

Disable container HTTP proxy inheritance as well as removing explicit container
and application proxy settings. With Podman use `--http-proxy=false`, or use
`HttpProxy=false` in Quadlet's `[Container]` section. These options stop host
proxy environment inheritance only. Inspect the installed Quadlet version and
generated service unit before relying on a drop-in. Render required
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
- [Garage 2.4.1 configuration source](https://docs.rs/crate/garage_util/2.4.1/source/config.rs)
- [Garage 2.4.1 socket address source](https://docs.rs/crate/garage_util/2.4.1/source/socket_address.rs)
- [SFTPGo configuration](https://docs.sftpgo.com/latest/config-file/)
- [Nginx proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
- [systemd path units](https://www.freedesktop.org/software/systemd/man/260/systemd.path.html)
- [systemd execution environment](https://www.freedesktop.org/software/systemd/man/260/systemd.exec.html)
- [Podman Quadlet 6.1.3](https://docs.podman.io/en/v6.1.3/markdown/podman-systemd.unit.5.html)
