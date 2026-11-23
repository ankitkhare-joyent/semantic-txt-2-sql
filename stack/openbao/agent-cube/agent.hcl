# Bao Agent sidecar for Cube: renders Cube's secret settings into an env file that
# Cube's entrypoint sources. Same pattern as agent-trino.
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
  source      = "/templates/cube.env.ctmpl"
  destination = "/rendered/cube.env"
  perms       = "0640"
}

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure         = true
}
