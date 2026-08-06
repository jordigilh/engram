Reviewers want succinct PR replies and docs: reference an existing
`REQ-*`/`AC-*`/`SC-*`/`DD-*` instead of restating it. Recurring feedback on
past PRs (`#12`-`#16`) clusters into the categories below -- self-check these
before opening or updating a PR to catch them before a reviewer has to:
- **Error mapping**: any local error path (decode/parse helpers) must return
  `grpcstatus.Errorf(codes.X, ...)`, never a bare `fmt.Errorf`/`errors.New` --
  otherwise it falls through the error mapper's default case as a wrong 500.
- **Response consistency**: if a resource has multiple return paths
  (fresh-create, idempotent-retry, error-recovery), every path must populate
  the same response fields -- diff the response construction across all of them.
- **Verify, don't assume**: never hardcode an assumption about an external
  system's data shape (map keys, IDs, enum values) from a field name alone --
  confirm it against the vendored proto or upstream source directly.
- **Comment/decision length**: a code comment or `DD-*` entry longer than
  ~4 lines is a smell -- if it restates a spec/decision recorded elsewhere,
  trim to a one-line reference instead.
- **Decisions vs. exploration**: `DD-*` entries in `.ai/decisions/` must be
  durable; implementation-detail research (dependency pins, one-off test
  recommendations) belongs in `.ai/exploration/` instead.
