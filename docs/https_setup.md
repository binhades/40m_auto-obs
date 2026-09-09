# Network access to the atlas web services

**Current state (2026-09-09): all four services are behind the nginx
reverse proxy** — `https://atlas/scheduler|uploader|controller|monitor/`,
port 443, TLS at nginx. Backends bind 127.0.0.1 and 8080–8083 are closed
in firewalld; 443/tcp is the only open port. This file documents how it
was built and how to maintain it. Sections below describe the two
generations: the nginx proxy (live) and the older per-service HTTPS
setup (superseded but still supported in code via cert auto-detect).

## nginx reverse proxy (LIVE)

- Canonical config: `docs/nginx_fast-obs.conf` in this repo = 
  `/etc/nginx/conf.d/fast-obs.conf` on atlas. Edit the repo copy, then:
  ```bash
  scp docs/nginx_fast-obs.conf atlas:/tmp/
  ssh atlas 'sudo install -m 644 /tmp/nginx_fast-obs.conf /etc/nginx/conf.d/fast-obs.conf && sudo nginx -t && sudo systemctl reload nginx'
  ```
- TLS cert (10 y, self-signed, CN=atlas, SAN IP:10.128.3.39):
  `/etc/pki/tls/certs/atlas.crt` + `/etc/pki/tls/private/atlas.key`
  (mode 600, root-owned). Regenerate the same way if the IP changes:
  ```bash
  sudo openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout /etc/pki/tls/private/atlas.key -out /etc/pki/tls/certs/atlas.crt \
    -subj "/CN=atlas" -addext "subjectAltName=DNS:atlas,IP:10.128.3.39"
  sudo chmod 600 /etc/pki/tls/private/atlas.key
  ```
- SELinux (Enforcing): `sudo setsebool -P httpd_can_network_connect on` —
  without this nginx's connects to the 127.0.0.1 backends are silently
  denied (this is the #1 suspect if every proxied request 502s).
- Firewalld (`work` zone on enp6s18):
  ```bash
  sudo firewall-cmd --zone=work --add-port=443/tcp --permanent
  sudo firewall-cmd --zone=work --remove-port=8080/tcp --permanent   # same for 8081-8083
  sudo firewall-cmd --reload
  ```
  To restrict 443 to the observatory subnet (controller drives hardware):
  ```bash
  sudo firewall-cmd --zone=work --add-rich-rule='rule family=ipv4 source address=10.128.0.0/16 port port=443 protocol=tcp accept' --permanent
  sudo firewall-cmd --reload
  ```
- Why sub-path routing "just works": NiceGUI builds every asset URL and
  the socket.io path (`${prefix}/_nicegui_ws/socket.io`) from the
  `X-Forwarded-Prefix` request header (verified in the installed 3.6.1
  source and empirically; same mechanism in 3.16). Each location must
  strip its prefix (trailing-slash `proxy_pass`!) and set
  `X-Forwarded-Prefix`; websockets need the `Upgrade` headers — all four
  services push UI updates over websocket.
- Gotcha: atlas's shell exports
  `http_proxy`/`https_proxy=socks5://10.128.3.20:1080`, so local curl
  tests tunnel through SOCKS and fail confusingly — always add
  `curl --noproxy '*'` on atlas.
- Service restarts need sudo: `sudo systemctl restart fast-obs-sche`
  etc. (polkit blocks plain `systemctl restart` as obs). Restart the
  controller only when no driver is running.

## Per-service HTTPS (superseded, still in the code)

Each service auto-detects a cert pair (`~/observe/certs/<service>.{crt,key}`)
and serves HTTPS when both exist, plain HTTP otherwise —
`ui.run(..., ssl_certfile=..., ssl_keyfile=...)` passes through to uvicorn
(verified on the atlas install, 3.6.1 and 3.16). **Behind the nginx proxy
the pairs must NOT exist**: a backend serving HTTPS would 502 the proxy,
which owns TLS. The historic per-service recipe, kept for the
TCP-passthrough topology (nginx `stream`) or direct-exposure fallback:

```bash
mkdir -p ~/observe/certs
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout ~/observe/certs/monitor.key -out ~/observe/certs/monitor.crt \
  -subj "/CN=atlas" \
  -addext "subjectAltName=DNS:atlas,IP:10.128.3.39"
chmod 600 ~/observe/certs/monitor.key
```

Self-signed = browsers warn once ("Advanced → Proceed"); the websocket
needs `https://` in the URL to work after accepting. Removing HTTPS:
delete/rename the pair and restart — the service falls back to HTTP.

## Notes

- Local repo testing: nicegui isn't installed locally, so proxy/HTTPS
  behavior is only exercised on atlas. The cert-presence detection is
  trivial and covered by /tmp/test_monitor_spectra.py structure.
- The controller GUI (v0.4.3) binds 127.0.0.1 and is reached via
  `https://atlas/controller/`. It DRIVES THE HARDWARE — when widening
  access beyond admin hosts, prefer a firewalld rich rule on 443 rather
  than reopening backend ports.

