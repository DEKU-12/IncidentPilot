output "frontend_url" {
  value = google_cloud_run_v2_service.shop["frontend"].uri
}

output "mcp_url" {
  value = google_cloud_run_v2_service.mcp.uri
}

output "agent_url" {
  value = google_cloud_run_v2_service.agent.uri
}

output "incidents_topic" {
  value = google_pubsub_topic.incidents.id
}
