package model

import (
	"fmt"

	"github.com/spf13/cobra"
	"k8s.io/klog/v2"
)

var templateName string

var listCmd = &cobra.Command{
	Use:   "list",
	Short: "List models for a given application template",
	Long:  ``,
	Args:  cobra.MaximumNArgs(0),
	RunE: func(cmd *cobra.Command, args []string) error {
		return list(cmd)
	},
}

func init() {
	listCmd.Flags().StringVarP(&templateName, "template", "t", "", "Application template name (Required)")
	listCmd.MarkFlagRequired("template")
}

func list(cmd *cobra.Command) error {
	models, err := models(templateName)
	if err != nil {
		return fmt.Errorf("failed to list the models, err: %w", err)
	}
	klog.Infoln("Models in application template", templateName, ":")
	for _, model := range models {
		klog.Infoln("-", model)
	}

	return nil
}
