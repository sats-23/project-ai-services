package image

import (
	"fmt"
	"slices"

	"github.com/project-ai-services/ai-services/internal/pkg/cli/templates"
	"github.com/project-ai-services/ai-services/internal/pkg/utils"
	"github.com/spf13/cobra"
)

var templateName string

var listCmd = &cobra.Command{
	Use:   "list",
	Short: "List container images for a given application template",
	Long:  ``,
	Args:  cobra.MaximumNArgs(0),
	RunE: func(cmd *cobra.Command, args []string) error {
		return list()
	},
}

func init() {
	listCmd.Flags().StringVarP(&templateName, "template", "t", "", "Application template name (Required)")
	listCmd.MarkFlagRequired("template")
}

func list() error {
	tp := templates.NewEmbedTemplateProvider(templates.EmbedOptions{})
	apps, err := tp.ListApplications()
	if err != nil {
		return fmt.Errorf("error listing templates: %w", err)
	}
	if found := slices.Contains(apps, templateName); !found {
		return fmt.Errorf("provided template name is wrong. Please provide a valid template name")
	}
	tmpls, err := tp.LoadAllTemplates(templateName)
	if err != nil {
		return fmt.Errorf("error loading templates for %s: %w", templateName, err)
	}

	dummyParams := map[string]any{
		"AppName": "dummy-app",
	}
	images := []string{}
	for _, tmpl := range tmpls {
		ps, err := tp.LoadPodTemplate(templateName, tmpl.Name(), dummyParams)
		if err != nil {
			return fmt.Errorf("error loading pod template: %w", err)
		}
		for _, container := range ps.Spec.Containers {
			images = append(images, container.Image)
		}
	}

	fmt.Printf("Container images for application template '%s' are:\n", templateName)
	for _, image := range utils.UniqueSlice(images) {
		fmt.Printf("- %s\n", image)
	}

	return nil
}
