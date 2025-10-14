package podman

import (
	"context"

	"github.com/containers/podman/v5/pkg/bindings"
	"github.com/containers/podman/v5/pkg/bindings/images"
)

type PodmanClient struct {
	Context context.Context
}

// NewPodmanClient creates and returns a new PodmanClient instance
func NewPodmanClient() (*PodmanClient, error) {
	ctx, err := bindings.NewConnectionWithIdentity(context.Background(), "ssh://root@127.0.0.1:62904/run/podman/podman.sock", "/Users/manjunath/.local/share/containers/podman/machine/machine", false)
	if err != nil {
		return nil, err
	}
	return &PodmanClient{Context: ctx}, nil
}

// Example function to list images (you can expand with more Podman functionalities)
func (pc *PodmanClient) ListImages() ([]string, error) {
	imagesList, err := images.List(pc.Context, nil)
	if err != nil {
		return nil, err
	}

	var imageNames []string
	for _, img := range imagesList {
		imageNames = append(imageNames, img.ID)
	}
	return imageNames, nil
}
