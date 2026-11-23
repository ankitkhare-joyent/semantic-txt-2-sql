# Cube's Bao Agent: read Cube's own settings (JWT audience/issuer, SQL API password). Nothing else.
path "secret/data/cube/*" {
  capabilities = ["read"]
}
