PROFILE    ?= ai-eventer
CONTEXT    ?= ai-eventer
NAMESPACE  ?= ai-eventer
RELEASE    ?= ai-eventer
AGENT_IMG  ?= ai-eventer-agent:dev
UI_IMG      ?= ai-eventer-ui:dev
WIN_KUBECONFIG ?= /mnt/c/Users/$(shell whoami)/.kube/$(PROFILE)-flat.yaml

# For a real cluster (EKS, self-hosted) instead of minikube: set REGISTRY to
# your registry (e.g. an ECR repo URI) and TAG to something other than
# ":dev" (that tag is meant for the local-only minikube loop above), then
# `make push`. This does NOT deploy anything - after pushing, install the
# chart yourself with --set agent.image.repository=$(REGISTRY)/ai-eventer-agent
# --set agent.image.tag=$(TAG) (and the same for ui.image.*), since only you
# know your cluster's context/namespace/release-name conventions.
REGISTRY   ?=
TAG        ?= dev

.PHONY: cluster-up cluster-down build load deploy undeploy logs-agent logs-ui kubeconfig-win push

cluster-up:
	bash infra/minikube/start.sh

cluster-down:
	minikube delete --profile $(PROFILE)

build:
	docker build -t $(AGENT_IMG) services/agent
	docker build -t $(UI_IMG) services/ui

push: build
	@if [ -z "$(REGISTRY)" ]; then echo "REGISTRY is required, e.g.: make push REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com/ai-eventer TAG=v0.1.0"; exit 1; fi
	docker tag $(AGENT_IMG) $(REGISTRY)/ai-eventer-agent:$(TAG)
	docker tag $(UI_IMG) $(REGISTRY)/ai-eventer-ui:$(TAG)
	docker push $(REGISTRY)/ai-eventer-agent:$(TAG)
	docker push $(REGISTRY)/ai-eventer-ui:$(TAG)
	@echo "Pushed. Install/upgrade with e.g.:"
	@echo "  helm upgrade --install ai-eventer ./chart --kube-context <your-context> --namespace ai-eventer --create-namespace \\"
	@echo "    --set agent.image.repository=$(REGISTRY)/ai-eventer-agent --set agent.image.tag=$(TAG) --set agent.image.pullPolicy=IfNotPresent \\"
	@echo "    --set ui.image.repository=$(REGISTRY)/ai-eventer-ui --set ui.image.tag=$(TAG) --set ui.image.pullPolicy=IfNotPresent"

load: build
	# `minikube image load` re-tags :dev in place by deleting the old image
	# under that name first. If a running pod still references it, that delete
	# fails ("must force") and minikube just warns and keeps the stale image -
	# so scale to 0 first (no container left holding the old image) rather
	# than delete-then-immediately-recreate, which would just recreate the
	# same conflict. `deploy` scales back up (both templates hardcode
	# replicas: 1, so `helm upgrade` re-asserts it) once the new image is in.
	kubectl --context $(CONTEXT) -n $(NAMESPACE) scale deployment/$(RELEASE)-agent deployment/$(RELEASE)-ui --replicas=0 2>/dev/null || true
	minikube image load $(AGENT_IMG) --profile $(PROFILE)
	minikube image load $(UI_IMG) --profile $(PROFILE)

deploy: load
	helm upgrade --install $(RELEASE) ./chart \
		--kube-context $(CONTEXT) \
		--namespace $(NAMESPACE) --create-namespace
	kubectl --context $(CONTEXT) -n $(NAMESPACE) rollout status deployment/$(RELEASE)-agent --timeout=120s
	kubectl --context $(CONTEXT) -n $(NAMESPACE) rollout status deployment/$(RELEASE)-ui --timeout=120s

undeploy:
	helm uninstall $(RELEASE) --kube-context $(CONTEXT) --namespace $(NAMESPACE)

logs-agent:
	kubectl --context $(CONTEXT) -n $(NAMESPACE) logs -l app.kubernetes.io/component=agent -f

logs-ui:
	kubectl --context $(CONTEXT) -n $(NAMESPACE) logs -l app.kubernetes.io/component=ui -f

# minikube's docker driver picks a new random host port for the API server on
# every `minikube start`, and the default kubeconfig points to Linux-absolute
# cert paths that Windows tools (e.g. Lens) can't resolve through the
# \\wsl.localhost\ file provider. This flattens certs inline and writes a
# Windows-filesystem copy that Lens/kubectl.exe can use directly. Re-run after
# every cluster-down + cluster-up (the port changes).
kubeconfig-win:
	mkdir -p "$(dir $(WIN_KUBECONFIG))"
	kubectl --context $(CONTEXT) config view --minify --flatten > "$(WIN_KUBECONFIG)"
	@echo "Wrote $(WIN_KUBECONFIG) - add it in Lens as: File > Add Cluster > browse to this path"
