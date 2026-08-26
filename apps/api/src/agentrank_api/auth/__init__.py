"""Merchant API credentials: how a caller proves which merchant it is acting for.

This is not user authentication. There is no person here, no password, no session and no
account. A merchant API credential is a machine credential held by whatever integration acts
on one merchant's behalf, and the only question it answers is which merchant a request is
scoped to. See SECURITY.md.
"""
