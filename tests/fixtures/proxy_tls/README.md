# Public loopback TLS fixtures

These are deliberately public, disposable test credentials. The server key has no production use.
The fixture CA signs only the loopback server certificate (localhost, 127.0.0.1, ::1).
The CA signing key was discarded. Both certificates are valid from 2025-01-01 to 2045-01-01.

Tests pass ca.crt explicitly to the client and keep certificate verification enabled.
They also require the same TLS endpoint to fail without this CA. Never install this CA in a
system trust store, and never use the server key outside the loopback tests.
