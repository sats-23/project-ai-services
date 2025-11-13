package helpers

import (
	"io"

	"github.com/jedib0t/go-pretty/v6/table"
)

type Printer struct {
	writer table.Writer
}

func NewTableWriter(out io.Writer) *Printer {
	t := table.NewWriter()
	t.SetOutputMirror(out)
	t.SetStyle(table.StyleRounded)

	return &Printer{
		writer: t,
	}
}

func (p *Printer) GetTableWriter() table.Writer {
	return p.writer
}

func (p *Printer) CloseTableWriter() {
	p.writer.ResetHeaders()
	p.writer.ResetRows()
}
