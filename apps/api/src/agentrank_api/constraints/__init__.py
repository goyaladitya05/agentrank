"""Authoritative semantic buyer constraints.

A `SpendingMandate` answers how much may be spent. An `IntentConstraintSet` answers what
may be bought. They are two independent authorization gates over one purchase, and both
have to allow before any payment can be considered safe.
"""
