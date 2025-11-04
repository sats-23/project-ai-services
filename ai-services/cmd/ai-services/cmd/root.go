package cmd

import (
	"os"

	"github.com/spf13/cobra"

	"github.com/project-ai-services/ai-services/cmd/ai-services/cmd/application"
	"github.com/project-ai-services/ai-services/cmd/ai-services/cmd/bootstrap"
	"github.com/project-ai-services/ai-services/cmd/ai-services/cmd/version"
)

// rootCmd represents the base command when called without any subcommands
var RootCmd = &cobra.Command{
	Use:     "ai-services",
	Short:   "AI Services CLI",
	Long:    `A CLI tool for managing AI services infrastructure.`,
	Version: version.GetVersion(),
	// Uncomment the following line if your bare application
	// has an action associated with it:
	// Run: func(cmd *cobra.Command, args []string) { },
}

// Execute adds all child commands to the root command and sets flags appropriately.
// This is called by main.main(). It only needs to happen once to the rootCmd.
func Execute() {
	err := RootCmd.Execute()
	if err != nil {
		os.Exit(1)
	}
}

func init() {
	RootCmd.AddCommand(version.VersionCmd)
	RootCmd.AddCommand(bootstrap.BootstrapCmd())
	RootCmd.AddCommand(application.ApplicationCmd)
}
