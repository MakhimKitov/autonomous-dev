// Atlas project config — schema authoring and the drift check.
//
// Atlas is a development instrument here, never a runtime dependency: it computes
// differences and it lints, and that is all. `control-plane migrate` still applies
// migrations, still owns `schema_migrations`, and still needs nothing installed —
// so the binary is required on a developer's machine and in CI, and on no server
// (ARCHITECTURE.md §12).
//
//   atlas schema diff --env local     # what would have to change to reach schema.sql
//
// An empty answer is the invariant: it means the migration directory and the
// declared schema agree. CI asserts it on every PR.

variable "dev_url" {
  type        = string
  description = "A throwaway database Atlas normalizes schema.sql in — it is written to and dropped."
  default     = "docker://postgres/18/dev"
}

env "local" {
  dev = var.dev_url

  // No `url` and no `src` here, deliberately. Supplying them would make
  // `atlas schema apply --env local` a working command — and that command
  // rewrites a database to match schema.sql directly, skipping the migration path
  // that is supposed to be the only way a schema changes. Left out, it fails for
  // want of a target instead. The commands we do use pass --from/--to explicitly
  // and cannot write anything.

  // schema_migrations is the runner's bookkeeping, not part of the data model:
  // it is created by `control-plane migrate` rather than by any migration, so it
  // must not read as drift in either direction. Schema-qualified because that is
  // what Atlas matches against.
  exclude = ["public.schema_migrations"]
}

// No `migration { dir = … }` block, deliberately. Atlas would want to own that
// directory — its own `atlas.sum` integrity file and its own timestamped file
// names — and the directory already has an owner with a different convention:
// `NNNN_slug.sql`, contiguous from 0001, enforced by the runner that applies it.
// Two conventions over one directory is worse than a paste, so Atlas computes the
// SQL and a human puts it in the next numbered file.
