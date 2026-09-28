# Security

Heftig holds very personal documents, so security reports are welcome and taken seriously.

**Please report vulnerabilities privately** via GitHub's “Report a vulnerability” (Security tab
of the repository) rather than in a public issue. Include what you found, how to reproduce it and
which version you used. You will get an answer within a week.

What Heftig does to protect the archive – and what it does not – is described in
[docs/architecture.md](docs/architecture.md) (security model) and
[docs/operations.md](docs/operations.md) (network access, HTTPS, backups). In short: Heftig is
meant to run on your own computer or home server, reachable only from your devices; don't expose
it to the internet without a VPN or a reverse proxy with TLS.
