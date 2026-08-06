# N7K2-R1.5 Active-Order Contract Fix

## Failure reproduced

The Stage-A regression used `IntegratedPaperHedgeApplication._active_orders()`
and then asserted on `.lifecycle` and `.approved_quantity`.  `_active_orders()`
is the planner-facing projection and intentionally returns
`planning.context.ActiveOrder`, whose `quantity` is the remaining order
quantity and which has no execution lifecycle.

## Remediation

- Added `_active_execution_orders()` returning non-terminal `ExecutionOrder`
  records from the execution control plane.
- Changed `_active_orders()` to project from that method.
- Changed the checkpoint/restore regression to compare execution lifecycle and
  approved quantity through `_active_execution_orders()`.

## Boundary preserved

No execution lifecycle type was added to `planning.context.ActiveOrder`.  This
prevents a reverse dependency from the pure planning domain into the execution
state machine.  Exchange writes remain hard-gated.
