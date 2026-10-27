module example.com/fixture

go 1.27

require (
	github.com/a/b v1.2.0
	github.com/c/d v0.3.0 // indirect
)

require golang.org/x/sys v0.30.0

require example.com/local v0.0.0

replace example.com/local => ./local
