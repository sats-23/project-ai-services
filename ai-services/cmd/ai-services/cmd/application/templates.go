package application

import (
	"fmt"
	"sort"

	"github.com/spf13/cobra"
	"k8s.io/klog/v2"

	"github.com/project-ai-services/ai-services/internal/pkg/cli/helpers"
)

var templatesCmd = &cobra.Command{
	Use:   "templates",
	Short: "Lists the offered application templates",
	Long:  `Retrieves information about the offered application templates`,
	RunE: func(cmd *cobra.Command, args []string) error {
		appTemplateNames, err := helpers.FetchApplicationTemplatesNames()
		if err != nil {
			return fmt.Errorf("failed to list application templates: %w", err)
		}

		if len(appTemplateNames) == 0 {
			cmd.PrintErrln("No application templates found.")
			return nil
		}

		// sort appTemplateNames alphabetically
		sort.Strings(appTemplateNames)

		klog.Infoln("Available Application Templates:")
		for _, name := range appTemplateNames {
			klog.Infoln("- ", name)
		}
		return nil
	},
}
