PY := .venv/bin/python
IP := $(PY) -m incidentpilot.cli

.PHONY: install test up traffic logs status clear eval-data eval deploy destroy cloud-urls

install:  ## install the package and dev tools into .venv
	$(PY) -m pip install -e ".[dev]"

test:  ## run the test suite
	$(PY) -m pytest -q

up:  ## start ShopDemo (frontend :8001, orders :8002, payments :8003)
	$(IP) up --fresh

traffic:  ## send ~5 req/s until Ctrl+C
	$(IP) traffic --rps 5

logs:  ## follow warnings and errors from every service
	$(IP) logs --severity WARNING -f

status:
	$(IP) chaos status

clear:
	$(IP) chaos clear

eval-data:  ## record 150 incidents with known root causes
	$(IP) eval generate -n 150

eval:  ## score the agent (MODEL=google_vertexai:gemini-3.8-flash to use Gemini)
	@test -d var/evals/dataset || $(IP) eval generate -n 150
	$(IP) eval run --model $(or $(MODEL),baseline)

# -- GCP --------------------------------------------------------------------------------
PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)
REGION  ?= us-central1
# Unique per deploy, so Cloud Run always rolls out the freshly built image (even with uncommitted changes).
IMAGE   := $(REGION)-docker.pkg.dev/$(PROJECT)/incidentpilot/app:$(shell git rev-parse --short HEAD)-$(shell date +%Y%m%d%H%M%S)
TF      := terraform -chdir=infra/terraform
TFVARS  := -var project_id=$(PROJECT) -var region=$(REGION)

deploy:  ## build the image with Cloud Build and deploy everything with Terraform
	$(TF) init -input=false
	$(TF) apply $(TFVARS) -target=google_project_service.apis -target=google_artifact_registry_repository.repo \
		-target=google_project_iam_member.build
	gcloud builds submit --project $(PROJECT) --region $(REGION) --config cloudbuild.yaml --substitutions _IMAGE=$(IMAGE) .
	$(TF) apply $(TFVARS) -var image=$(IMAGE)

cloud-urls:  ## print the deployed service URLs
	$(TF) output

destroy:  ## delete every cloud resource this project created
	$(TF) destroy $(TFVARS) -var image=$(IMAGE)
