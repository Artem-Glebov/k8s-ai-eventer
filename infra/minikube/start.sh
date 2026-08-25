#!/usr/bin/env bash
# Starts a dedicated minikube profile for local dev/test of ai-k8s-eventer.
# Uses its own profile name so it never touches any other cluster/context
# already configured on this machine.
set -euo pipefail

PROFILE="${PROFILE:-ai-eventer}"
DRIVER="${DRIVER:-docker}"
CPUS="${CPUS:-4}"
MEMORY="${MEMORY:-6144}" # MB

minikube start \
  --profile "$PROFILE" \
  --driver "$DRIVER" \
  --cpus "$CPUS" \
  --memory "$MEMORY"

# metrics-server backs node/pod capacity-aware advice (Phase 2 rule checks).
minikube addons enable metrics-server --profile "$PROFILE"

echo "Cluster ready. Use: kubectl --context ${PROFILE} get nodes"
