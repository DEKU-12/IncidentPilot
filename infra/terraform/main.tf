terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
    time   = { source = "hashicorp/time", version = "~> 0.12" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {}

locals {
  number = data.google_project.this.number
  shop   = toset(["frontend", "orders", "payments"])
  # Cloud Run's deterministic URLs, so services can point at each other without a dependency cycle.
  shop_url = { for s in local.shop : s => "https://shopdemo-${s}-${local.number}.${var.region}.run.app" }
  mcp_url  = "https://incidentpilot-mcp-${local.number}.${var.region}.run.app"
  apis = [
    "run.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com", "pubsub.googleapis.com", "logging.googleapis.com",
    "monitoring.googleapis.com", "aiplatform.googleapis.com", "iam.googleapis.com",
    "billingbudgets.googleapis.com",
  ]
  shop_env = merge(
    { for s in local.shop : "${upper(s)}_URL" => local.shop_url[s] },
    { GOOGLE_CLOUD_PROJECT = var.project_id },
  )
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# Newly enabled APIs take a minute to work everywhere; using them sooner fails with SERVICE_DISABLED.
resource "time_sleep" "apis_ready" {
  depends_on      = [google_project_service.apis]
  create_duration = "60s"
}

# -- image registry and build identity --------------------------------------------

resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "incidentpilot"
  format        = "DOCKER"
  depends_on    = [time_sleep.apis_ready]
}

resource "google_service_account" "build" {
  account_id   = "incidentpilot-build"
  display_name = "IncidentPilot Cloud Build"
  depends_on   = [time_sleep.apis_ready]
}

resource "google_project_iam_member" "build" {
  for_each = toset(["roles/artifactregistry.writer", "roles/logging.logWriter", "roles/storage.objectViewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.build.email}"
}

# -- secrets ----------------------------------------------------------------------

resource "random_password" "secret" {
  for_each = toset(["admin", "approval"])
  length   = 40
  special  = false
}

resource "google_secret_manager_secret" "secret" {
  for_each   = { admin = "shopdemo-admin-token", approval = "incidentpilot-approval-secret" }
  secret_id  = each.value
  depends_on = [time_sleep.apis_ready]
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "secret" {
  for_each    = toset(["admin", "approval"])
  secret      = google_secret_manager_secret.secret[each.key].id
  secret_data = random_password.secret[each.key].result
}

# -- service accounts (least privilege) --------------------------------------------

resource "google_service_account" "sa" {
  for_each     = { shop = "ShopDemo services", mcp = "IncidentPilot MCP server", agent = "IncidentPilot agent", push = "Pub/Sub push to the agent" }
  account_id   = each.key == "shop" ? "shopdemo" : "incidentpilot-${each.key}"
  display_name = each.value
  depends_on   = [time_sleep.apis_ready]
}

resource "google_project_iam_member" "mcp_logs" {
  project = var.project_id
  role    = "roles/logging.viewer"
  member  = "serviceAccount:${google_service_account.sa["mcp"].email}"
}

resource "google_project_iam_member" "agent_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.sa["agent"].email}"
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each = {
    shop_admin   = ["shop", "admin"]
    mcp_admin    = ["mcp", "admin"]
    mcp_approval = ["mcp", "approval"]
  }
  secret_id = google_secret_manager_secret.secret[each.value[1]].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.sa[each.value[0]].email}"
}

# -- ShopDemo: public demo services; the admin API needs the secret token --------------

resource "google_cloud_run_v2_service" "shop" {
  for_each            = local.shop
  name                = "shopdemo-${each.key}"
  location            = var.region
  deletion_protection = false
  depends_on          = [google_secret_manager_secret_iam_member.access, google_secret_manager_secret_version.secret]

  template {
    service_account = google_service_account.sa["shop"].email
    scaling {
      min_instance_count = 0
      max_instance_count = 1 # revisions and faults live in memory: keep one instance
    }
    containers {
      image = var.image
      env {
        name  = "ROLE"
        value = each.key
      }
      dynamic "env" {
        for_each = local.shop_env
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name = "SHOPDEMO_ADMIN_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secret["admin"].secret_id
            version = "latest"
          }
        }
      }
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "shop_public" {
  for_each = local.shop
  name     = google_cloud_run_v2_service.shop[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# -- MCP server: private; reads Cloud Logging, calls the shop admin API ------------------

resource "google_cloud_run_v2_service" "mcp" {
  name                = "incidentpilot-mcp"
  location            = var.region
  deletion_protection = false
  depends_on          = [google_secret_manager_secret_iam_member.access, google_secret_manager_secret_version.secret]

  template {
    service_account = google_service_account.sa["mcp"].email
    scaling {
      max_instance_count = 3
    }
    containers {
      image = var.image
      dynamic "env" {
        for_each = merge(local.shop_env, { ROLE = "mcp", INCIDENTPILOT_BACKEND = "gcp" })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = { SHOPDEMO_ADMIN_TOKEN = "admin", APPROVAL_SECRET = "approval" }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secret[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "agent_calls_mcp" {
  name     = google_cloud_run_v2_service.mcp.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa["agent"].email}"
}

# -- agent: private; woken by Pub/Sub, investigates read-only --------------------------

resource "google_cloud_run_v2_service" "agent" {
  name                = "incidentpilot-agent"
  location            = var.region
  deletion_protection = false

  template {
    service_account = google_service_account.sa["agent"].email
    timeout         = "900s"
    scaling {
      max_instance_count = 2
    }
    containers {
      image = var.image
      dynamic "env" {
        for_each = {
          ROLE                  = "agent"
          MCP_URL               = local.mcp_url
          INCIDENTPILOT_MODEL   = var.model
          GOOGLE_CLOUD_PROJECT  = var.project_id
          GOOGLE_CLOUD_LOCATION = "global"
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "push_calls_agent" {
  name     = google_cloud_run_v2_service.agent.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa["push"].email}"
}

# -- alert -> Pub/Sub -> agent ------------------------------------------------------------

resource "google_pubsub_topic" "incidents" {
  name       = "incidentpilot-incidents"
  depends_on = [time_sleep.apis_ready]
}

resource "google_pubsub_subscription" "agent_push" {
  name                 = "incidentpilot-agent-push"
  topic                = google_pubsub_topic.incidents.id
  ack_deadline_seconds = 600
  push_config {
    push_endpoint = "${google_cloud_run_v2_service.agent.uri}/incident"
    oidc_token {
      service_account_email = google_service_account.sa["push"].email
      audience              = google_cloud_run_v2_service.agent.uri # the service URL, not the /incident path
    }
  }
  retry_policy {
    minimum_backoff = "60s"
  }
  expiration_policy {
    ttl = ""
  }
}

resource "google_logging_metric" "frontend_5xx" {
  name   = "shopdemo_frontend_5xx"
  filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"shopdemo-frontend\" AND jsonPayload.http.status>=500"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [time_sleep.apis_ready]
}

resource "google_monitoring_notification_channel" "pubsub" {
  display_name = "IncidentPilot agent"
  type         = "pubsub"
  labels       = { topic = google_pubsub_topic.incidents.id }
}

# Cloud Monitoring creates its notification service agent with the first Pub/Sub channel.
resource "google_pubsub_topic_iam_member" "monitoring_publishes" {
  topic      = google_pubsub_topic.incidents.name
  role       = "roles/pubsub.publisher"
  member     = "serviceAccount:service-${local.number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
  depends_on = [google_monitoring_notification_channel.pubsub]
}

resource "google_monitoring_alert_policy" "frontend_5xx" {
  display_name          = "ShopDemo frontend 5xx errors"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.pubsub.id]

  conditions {
    display_name = "frontend 5xx per minute above ${var.alert_threshold}"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.frontend_5xx.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.alert_threshold
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content = "IncidentPilot investigates automatically. Reports: Logs Explorer, jsonPayload.message=\"rca_report\"."
  }
}

# -- optional budget alert ------------------------------------------------------------------

resource "google_billing_budget" "budget" {
  count           = var.billing_account == "" ? 0 : 1
  billing_account = var.billing_account
  display_name    = "IncidentPilot"

  budget_filter {
    projects = ["projects/${local.number}"]
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }
  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }
}
