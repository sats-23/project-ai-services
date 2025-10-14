package main

import "github.com/project-ai-services/ai-services/cmd/ai-services/cmd"

// ai-services completion bash|zsh|fish|powershell
// ai-services --version
// ai-services adm controlplane --help
// ai-services adm controlplane bootstrap
// ai-services adm controlplane reset
// ai-services adm controlplane status
// ai-services adm controlplane upgrade // TODO:GA2
// ai-services adm controlplane backup // TODO:GA2
// ai-services adm worker --help
// ai-services adm worker bootstrap
// ai-services adm worker reset
// ai-services adm worker status
// ai-services adm worker upgrade // TODO:GA2
// ai-services adm config //TODO: if needed
// ai-services server --help

func main() {
	cmd.Execute()
}
