{{- define "agentic.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agentic.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* labels without selector keys: safe to combine with agentic.selector in pod templates */}}
{{- define "agentic.metaLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: agentic-ai-platform
{{- end -}}

{{- define "agentic.labels" -}}
app.kubernetes.io/name: {{ include "agentic.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{ include "agentic.metaLabels" . }}
{{- end -}}

{{- define "agentic.selector" -}}
app.kubernetes.io/name: {{ include "agentic.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "agentic.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agentic.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* image reference: digest wins over tag; prod refuses tag-only deployments */}}
{{- define "agentic.imageRef" -}}
{{- $img := .image -}}
{{- $repo := $img.repository -}}
{{- if .registry }}{{ $repo = printf "%s/%s" .registry $img.repository }}{{ end -}}
{{- if $img.digest -}}
{{- printf "%s@%s" $repo $img.digest -}}
{{- else -}}
{{- if eq .env "prod" -}}
{{- fail "image digest is required in prod (image.digest=sha256:...)" -}}
{{- end -}}
{{- printf "%s:%s" $repo (default .appVersion $img.tag) -}}
{{- end -}}
{{- end -}}

{{- define "agentic.apiImage" -}}
{{- include "agentic.imageRef" (dict "image" .Values.image "registry" .Values.image.registry "env" .Values.environment "appVersion" .Chart.AppVersion) -}}
{{- end -}}

{{- define "agentic.secretName" -}}
{{- if .Values.secrets.externalSecrets.enabled -}}
{{- printf "%s-secrets" (include "agentic.fullname" .) -}}
{{- else -}}
{{- .Values.secrets.existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "agentic.qdrantUrl" -}}
{{- if .Values.qdrant.internal.enabled -}}
{{- printf "http://%s-qdrant:6333" (include "agentic.fullname" .) -}}
{{- else -}}
{{- required "config.agent.qdrantUrl is required when the internal Qdrant is disabled" .Values.config.agent.qdrantUrl -}}
{{- end -}}
{{- end -}}

{{- define "agentic.pvcName" -}}
{{- default (printf "%s-state" (include "agentic.fullname" .)) .Values.persistence.existingClaim -}}
{{- end -}}

{{/* shared env for API and CronJob */}}
{{- define "agentic.env" -}}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: FASTEMBED_CACHE_PATH
  value: /var/lib/agentic/models
- name: AGENTIC_SECRETS_DIR
  value: /var/run/secrets/agentic
{{- range .Values.config.extraEnv }}
- {{ toYaml . | nindent 2 | trim }}
{{- end }}
{{- end -}}

{{- define "agentic.envFrom" -}}
- configMapRef:
    name: {{ include "agentic.fullname" . }}-config
{{- end -}}

{{- define "agentic.secretOptional" -}}
{{- not (or .Values.secrets.externalSecrets.enabled (has .Values.environment (list "staging" "prod"))) -}}
{{- end -}}

{{- define "agentic.volumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: 512Mi
- name: state
{{- if .Values.persistence.enabled }}
  persistentVolumeClaim:
    claimName: {{ include "agentic.pvcName" . }}
{{- else }}
  emptyDir:
    sizeLimit: 2Gi
{{- end }}
- name: policy
  configMap:
    name: {{ include "agentic.fullname" . }}-policy
- name: secrets
  secret:
    secretName: {{ include "agentic.secretName" . }}
    defaultMode: 0440
    optional: {{ include "agentic.secretOptional" . }}
{{- end -}}

{{- define "agentic.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: state
  mountPath: /var/lib/agentic
- name: policy
  mountPath: /etc/agentic/policy
  readOnly: true
- name: secrets
  mountPath: /var/run/secrets/agentic
  readOnly: true
{{- end -}}
