{{- define "ai-eventer.fullname" -}}
{{- if contains "ai-eventer" .Release.Name -}}
{{ .Release.Name }}
{{- else -}}
{{ .Release.Name }}-ai-eventer
{{- end -}}
{{- end -}}

{{- define "ai-eventer.labels" -}}
app.kubernetes.io/name: ai-eventer
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ai-eventer.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "ai-eventer.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Non-root pod-level hardening for the two workloads we control the image/runtime for
(agent, ui) - both are plain python:3.12-slim with no baked-in user, so an arbitrary
non-root UID works with no Dockerfile changes needed.
*/}}
{{- define "ai-eventer.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 1000
runAsGroup: 1000
fsGroup: 1000
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/*
Container-level hardening applied to every container in the chart, including ollama's
upstream image - deliberately does NOT force a non-root user there (left to run as
whatever the upstream image defaults to), since we don't control that image's internals
and a broken model load is worse than one extra open door to a still-capabilities-dropped,
read-only-rootfs, no-privilege-escalation container.
*/}}
{{- define "ai-eventer.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop:
    - ALL
{{- end -}}
