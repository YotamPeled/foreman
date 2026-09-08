# Plan: harbor-tally

The owner wants one trusted invoice run. Three tasks: two independent
collection passes (log transcription, dock walk) that can run in parallel,
then a reconcile-and-invoice task after both. The reconcile task is marked
core because money arithmetic is the work correctness matters for.

Monitors track the two collection passes so the supervisor sees progress
before the reconcile task starts. The privacy rule keeps owner names out
of findings and invoices alike.
