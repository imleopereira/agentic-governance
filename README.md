# Code Atelier Governance SDK

Enterprise-grade governance for AI agent deployments. Audit trails, approval gates,
scope enforcement, spend limits — all in one pip install.

```python
from codeatelier_governance import GovernanceSDK

sdk = GovernanceSDK(database_url="postgresql://localhost/myapp")

# That's it. Audit logging is now active.
```

## Features (v0.1)
- Decision audit trail (immutable, append-only)
- Step-level provenance (trace any output to its inputs)
- Prompt versioning (track, diff, rollback)
- Action scope enforcement (whitelist tools and APIs per agent)
- Spend limits (token and dollar budgets per agent/session/client)
- Human-in-the-loop gates (configurable approval thresholds)

## Install
```bash
pip install codeatelier-governance
```
