# Selected library startup root

Owner: Arendt. Bounded follow-up after merged443 f124db8b. The original live R1
worker already supplies the correct launch root. This continuation closes the
selected library caller that supplies its explicit maintenance_root without an
ambient AGENT_COMMS_ROOT. NativeStartupAdmission.for_launch remains the sole
factory; the tracked launch passes its existing original root to that factory.

Verify only that concrete library call and acquired startup lease. Do not repeat
the accepted native startup, CPU, cancellation or whole-turn cohorts. No change
to readiness budgets, durable input dispositions, historical UNKNOWN, retry or
native protocol. The full C3 phased cutover remains442's independent priority.

Draft receiving checkpoint; the preserved five-line source refinement is not
yet claimed tested or ready.
