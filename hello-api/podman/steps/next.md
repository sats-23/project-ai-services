{{ .AppName }} is deployed.

- API endpoint: http://{{ .HOST_IP }}:{{ .HELLO_API_PORT }}/greeting.json
- Test it: curl http://{{ .HOST_IP }}:{{ .HELLO_API_PORT }}/greeting.json
