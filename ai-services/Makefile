GO := go
BUILDTAGS := "exclude_graphdriver_btrfs containers_image_openpgp remote"
BIN = ./bin

# This will be overriden when we build and upload binary to artifact
VERSION ?= $(shell git describe --tags --always)
GITCOMMIT ?= $(shell git rev-parse --short HEAD)
BUILDDATE ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

.PHONY: build
build:
	CGO_ENABLED=0 $(GO) build $(BUILDFLAGS) \
	-o $(BIN)/ai-services \
	-tags $(BUILDTAGS) \
	-ldflags " \
    -X 'github.com/project-ai-services/ai-services/cmd/ai-services/cmd/version.Version=$(VERSION)' \
    -X 'github.com/project-ai-services/ai-services/cmd/ai-services/cmd/version.GitCommit=$(GITCOMMIT)' \
    -X 'github.com/project-ai-services/ai-services/cmd/ai-services/cmd/version.BuildDate=$(BUILDDATE)' \
  	" \
	./cmd/ai-services
