package application

import (
	"fmt"
	"sort"

	"github.com/spf13/cobra"

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

		appTemplateNameswithCustomArgs, err := helpers.FetchCustomArgsFromMetadata(appTemplateNames)

		// sort custom args for each application template
		for _, name := range appTemplateNames {
			sort.Strings(appTemplateNameswithCustomArgs[name])
		}

		cmd.Println("Available Application Templates:")
		for _, name := range appTemplateNames {
			cmd.Println("- ", name)
			values := appTemplateNameswithCustomArgs[name]

			for i, v := range values {
				if i == len(values)-1 {
					cmd.Println("   └─ ", v) // last item
				} else {
					cmd.Println("   ├─ ", v) // middle items
				}
			}
		}
		return nil
	},
}
