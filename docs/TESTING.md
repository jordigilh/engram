# Testing contract

Engram uses a practical test-pyramid invariant for business-logic changes:

1. **Unit tier:** deterministic policy and routing logic is tested without
   network, Postgres, containers, or real MCP daemons. The CI gate is **at
   least 90% line coverage** for the managed zvec policy module and the Bun
   plugin suite. The current remediation surface is covered at 99% for Python
   zvec policy and 91% for Bun lines.
2. **Integration tier:** gateway/backend and database wiring is tested in the
   dedicated integration job. Unit tests must not be replaced by integration
   tests merely to raise coverage.
3. **Deployment-smoke tier:** image digest verification, container health,
   MCP initialization, route order (`7999 -> 8898 -> 8896`), and rollback are
   verified during deployment/release validation rather than mocked into unit
   tests.

The invariant is a minimum distribution rule, not a claim that every line of
legacy transport, platform bootstrap, or third-party adapter code has 100%
coverage. New deterministic business logic must remain in the unit tier and
must not lower its 90% gate.
