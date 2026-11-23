# OpenBao server config: production layout, local scale.
#
# What differs from a real deployment (docs/03-openbao.md has the details):
#   * TLS is off on the listener. In production every hop to OpenBao is TLS.
#   * One node. In production you run 3 or 5 nodes with Raft so a node can fail.
#   * Unsealing is manual (Shamir keys, done by scripts/bao-bootstrap.sh). In production
#     you use auto-unseal with a cloud KMS or HSM (`seal "awskms" {...}`), so nobody
#     holds unseal keys and a restarted pod unseals itself.

ui            = true
disable_mlock = true          # mlock needs IPC_LOCK; Raft's own storage makes this the recommended setting

storage "raft" {              # integrated storage: no Consul or external DB to run
  path    = "/openbao/file"
  node_id = "bao-0"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true          # local only
}

api_addr     = "http://openbao:8200"
cluster_addr = "http://openbao:8201"

# Short default token lifetimes: a leaked token is useless within the hour.
default_lease_ttl = "1h"
max_lease_ttl     = "24h"

telemetry {
  disable_hostname          = true
  prometheus_retention_time = "1h"
}

# Audit log: every request and response, with secret values HMAC'd (comparable, not readable).
# OpenBao only allows audit devices declared here, not created over the API: an attacker
# with a root token can't quietly switch auditing off or redirect it.
audit "file" "file" {
  description = "Local audit log (ship it to your SIEM in production)."
  options {
    file_path = "/openbao/logs/audit.log"
  }
}
