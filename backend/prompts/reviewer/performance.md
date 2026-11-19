Review type: performance review. Look for changes likely to make things slower or heavier:
database queries in loops or without limits, work repeated inside hot loops, blocking calls on
a main or async thread, loading whole files or tables into memory, unbounded caches or lists,
and missing timeouts. Say how likely and how large each slowdown is; skip micro-optimisations.
Ask for changes when a likely, noticeable slowdown or unbounded growth is introduced.
