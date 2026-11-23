# Bao Agent sidecar for Trino.
# It logs in with Trino's AppRole, keeps its token renewed, and renders Trino's Iceberg
# catalog file from a template. Trino itself never talks to OpenBao and never sees a token.
# In Kubernetes this is the same binary as an init/sidecar container (or the
# OpenBao/External Secrets operators doing the equivalent).
vault {
  address = "http://openbao:8200"
}

auto_auth {
  method "approle" {
    config = {
      role_id_file_path                   = "/approle/role_id"
      secret_id_file_path                 = "/approle/secret_id"
      remove_secret_id_file_after_reading = false
    }
  }
}

template {
  source      = "/templates/lake.properties.ctmpl"
  destination = "/rendered/lake.properties"
  perms       = "0640"
  # Trino reads catalogs at startup, so a changed secret needs a Trino restart.
  # (Dynamic catalogs or a file-watching process would avoid that.)
}

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure         = true
}
