// Command fixture. Comments and blank lines are not code.
package main

import (
	"fmt"

	"example.com/local"
)

/*
A block comment.
*/
func main() { a := local.Double(1); b := 2; fmt.Println(a + b) }
