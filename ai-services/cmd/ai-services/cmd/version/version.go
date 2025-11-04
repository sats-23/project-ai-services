package version

import (
	"github.com/spf13/cobra"
)

var (
	// Below values will be overriden during build
	Version   string = "unknown"
	GitCommit string = "unknown"
	BuildDate string = ""
)

func GetVersion() string {
	return Version
}

var VersionCmd = &cobra.Command{
	Use:   "version",
	Short: "Prints CLI version with more info",
	Run: func(cmd *cobra.Command, args []string) {
		cmd.Printf("Version: %s\nGitCommit: %s\nBuildDate: %s\n", Version, GitCommit, BuildDate)
	},
}
