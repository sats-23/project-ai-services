package adm

import (
	"os"

	log "github.com/project-ai-services/ai-services/internal/pkg/logger"
	"github.com/spf13/cobra"
	"go.uber.org/zap"
)

var (
	logger = log.GetLogger()
)

// boostrapCmd represents the bootstrap command
var boostrapCmd = &cobra.Command{
	Use:   "bootstrap",
	Short: "A brief description of your command",
	Long: `A longer description that spans multiple lines and likely contains examples
and usage of using your command. For example:

Cobra is a CLI library for Go that empowers applications.
This application is a tool to generate the needed files
to quickly create a Cobra application.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		return bootstrap()
	},
}

func bootstrap() error {
	logger.Info("bootstrap called")
	if err := rootCheck(); err != nil {
		return err
	}
	return nil
}

func rootCheck() error {
	euid := os.Geteuid()

	if euid == 0 {
		logger.Info("✅ Current user is root.")
	} else {
		logger.Info("❌ Current user is not root.")
		logger.Info("Effective User ID", zap.Int("euid", euid))
	}
	return nil
}
