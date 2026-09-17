Day N:

{{- if eq .HELLO_API_STATUS "running" }}

- {{ .AppName }} API is available at http://{{ .HOST_IP }}:{{ .HELLO_API_PORT }}/greeting.json
{{- else }}

- {{ .AppName }} API is unavailable. Please make sure the 'hello-api' pod is running.
{{- end }}
