"""Process-wide serialization for component-dependency mutations.

Deleting a skill/KB scans deployed pipelines for references, then deletes; a
concurrent deploy that adds a reference in that window would leave a deployed
pipeline pointing at a just-deleted resource (review finding 3 -- FastAPI runs
sync endpoints in a threadpool, so this races even with one worker). Holding
this lock across BOTH the delete's scan+delete+commit and a deploy's
reference-resolution+write+commit makes them mutually exclusive:

- deploy-then-delete: the delete's scan sees the new reference and 409s.
- delete-then-deploy: the deploy's reference resolution fails (resource gone)
  and 400s.

Either way no orphaned deployed pipeline results. This is an interim mitigation;
the durable fix is persisted dependency rows + DB ``RESTRICT`` (P1-04). It is
process-wide -- a multi-worker deployment would need a cross-process lock, the
same single-worker constraint the autonomous email-trigger poller documents.
"""

import threading

component_mutation_lock = threading.Lock()
