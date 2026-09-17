Day N:

{{- if eq .HELLO_API_STATUS "running" }}

- {{ .SERVICE_NAME }} API is available at {{ .API_URL }}/greeting.json
{{- else }}

- {{ .SERVICE_NAME }} API is unavailable. Please make sure the 'hello-api' pod is running.
{{- end }}
