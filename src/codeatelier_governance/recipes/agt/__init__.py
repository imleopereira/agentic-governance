"""Microsoft AGT (Agentic Governance Tool) recipe scaffold.

Microsoft's Agent Framework shipped a governance competitor (DevUI +
Aspire + first-class HITL) in 2026. This recipe scaffolds a ready-to-
run Python project where every AGT tool call is wrapped by the SDK's
``scope.check`` + ``cost.check_or_raise`` + ``gates.request`` pipeline.

The Microsoft AGT import is deliberately commented out in the rendered
``agent.py`` — we don't want ``pip install code-atelier-governance``
to drag ``microsoft-agentic`` in transitively just to ship a scaffold.
"""
