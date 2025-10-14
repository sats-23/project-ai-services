package cmd

import (
	"os"

	"github.com/project-ai-services/ai-services/cmd/ai-services/cmd/adm"
	"github.com/project-ai-services/ai-services/cmd/ai-services/cmd/server"
	"github.com/spf13/cobra"
)

var Version = "dev"

// rootCmd represents the base command when called without any subcommands
var RootCmd = &cobra.Command{
	Use:   "ai-services",
	Short: "A brief description of your application",
	Long: `A longer description that spans multiple lines and likely contains
examples and usage of using your application. For example:

Cobra is a CLI library for Go that empowers applications.
This application is a tool to generate the needed files
to quickly create a Cobra application.`,
	Version: Version,
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
	RootCmd.AddCommand(adm.AdmCmd)
	RootCmd.AddCommand(server.ServerCmd)
}
