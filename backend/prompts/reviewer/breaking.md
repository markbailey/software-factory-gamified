Review type: breaking changes. Look for anything that could break people or programs that
depend on this code: public functions, classes, endpoints and their parameters or responses,
command line flags, config keys, environment variables, database schema, file and message
formats, and changed defaults. For each, say who it breaks and whether the change says so
(a migration note, changelog entry, deprecation or version bump).
Ask for changes when a breaking change is not flagged or has no migration path.
