variable "project_id" {
  type        = string
  description = "GCP project to deploy into."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "image" {
  type        = string
  default     = ""
  description = "Container image for every service (built by `make deploy`)."
}

variable "model" {
  type    = string
  default = "google_vertexai:gemini-3.8-flash"
}

variable "alert_threshold" {
  type        = number
  default     = 5
  description = "Frontend 5xx responses per minute that fire the alert and wake the agent."
}

variable "billing_account" {
  type        = string
  default     = ""
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX). Set it to create a budget alert."
}

variable "budget_usd" {
  type    = number
  default = 50
}
