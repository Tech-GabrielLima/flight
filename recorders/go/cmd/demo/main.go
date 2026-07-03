// Command demo records a tiny run and writes a `.flight` — the same black-box
// format the Python/Rust engine reads. Run it as:
//
//	go run ./cmd/demo /path/to/out.flight
//
// Then, from Python:  flight.read("/path/to/out.flight")  — or  flight inspect.
package main

import (
	"fmt"
	"os"

	"github.com/gabriellima/flight-go/flight"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: demo <out.flight>")
		os.Exit(2)
	}
	out := os.Args[1]

	rec := flight.New()
	// Two "code objects" (functions) and a handful of events across them.
	rec.RegisterCode(1, "orders.go", "main", 10)
	rec.RegisterCode(2, "orders.go", "processRefund", 42)

	rec.Record(flight.KindStart, 0, 0, 1)  // main() started
	rec.Record(flight.KindStart, 0, 42, 2) // processRefund() started
	rec.Record(flight.KindLine, 0, 47, 2)  // a line ran
	rec.Record(flight.KindRaise, 0, 47, 2) // an error was raised
	rec.Record(flight.KindUnwind, 0, 42, 2)
	rec.Record(flight.KindUnwind, 0, 10, 1)

	if err := rec.Dump(out, flight.DefaultMeta()); err != nil {
		fmt.Fprintln(os.Stderr, "dump failed:", err)
		os.Exit(1)
	}
	fmt.Printf("wrote %s (%d events)\n", out, rec.Len())
}
