"""Deterministic diagnostics over benchmark evidence.

The diagnostics layer turns what the benchmark, the traces, the compiler and the commerce
kernel already established into merchant readable findings. It is evidence first: every
diagnosis is derived from persisted trusted rows by stated rules, never from model prose,
and every attribution carries its evidence level so a trusted fact, a deterministic
attribution and an honest unknown never blur together.

Diagnostics are derived on read with an explicit engine identity rather than persisted.
Historical evidence stays immutable; a diagnosis is a function of that evidence plus the
engine version that read it, and the identity travels in the output so historical reports
remain interpretable against the logic that produced them.

Nothing in this package writes. It recommends; the authoritative workflows act.
"""
