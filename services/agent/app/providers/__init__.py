"""Provider adapters used by the Step-4 capabilities.

Providers talk to the outside world; capabilities translate the result into
the Agent contract (Observation payload, evidence, model accounting). The
separation keeps the kernel free of HTTP details and keeps every external
call replaceable in tests.
"""
