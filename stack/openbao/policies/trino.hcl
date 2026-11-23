# Trino's Bao Agent: read the object-store keys it renders into the Iceberg catalog file. Nothing else.
path "secret/data/lakehouse/s3" {
  capabilities = ["read"]
}
