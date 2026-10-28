"""Use cases and the pure evaluator.

A service receives a connection and a principal, checks the command, and
calls repository functions. The service method owns the transaction for a
write. This package does not open a database connection.
"""
