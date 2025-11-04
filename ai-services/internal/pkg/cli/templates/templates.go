package templates

import (
	"text/template"

	v1 "github.com/containers/podman/v5/pkg/k8s.io/api/core/v1"
)

type AppMetadata struct {
	// TODO: Include other variables too
	SMTLevel              *int       `yaml:"smtLevel,omitempty"`
	PodTemplateExecutions [][]string `yaml:"podTemplateExecutions"`
}

type PodSpec struct {
	v1.Pod
}

type Template interface {
	// ListApplications lists all available application templates
	ListApplications() ([]string, error)
	// LoadAllTemplates loads all templates for a given application
	LoadAllTemplates(app string) (map[string]*template.Template, error)
	// LoadPodTemplate loads and renders a pod template with the given parameters
	LoadPodTemplate(app, file string, params any) (*PodSpec, error)
	// LoadMetadata loads the metadata for a given application template
	LoadMetadata(app string) (*AppMetadata, error)
}
