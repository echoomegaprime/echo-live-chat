# Contributing

Thank you for improving Echo Live Chat. This is a public, proprietary support-chat runtime:
contributions are welcome for review, but repository visibility does not grant a general use or
redistribution license.

## Development path

1. Open an issue describing the tenant-safety or chat-runtime invariant being improved.
2. Create a focused branch from current `main`.
3. Add a failing test before changing a tenant-isolation, admin-authority, visitor-session, or
   Stripe-webhook-verification boundary.
4. Run the complete local verification suite (`pytest`) before opening a pull request.
5. Open a pull request using the repository template and include exact test output.

## Pull-request requirements

- No secrets, private evidence, customer data, chat text, or personal information are included.
- No stubs, placeholders, skipped security checks, or self-asserted readiness.
- Public behavior and deployment changes include documentation and negative-path tests.
