"""EU AI Act Article 12 evidence report generation from audit trail data.

Generates evidence reports for actions the SDK observed. These reports do not
assert compliance — Article 12 compliance for a deployment depends on routing
all relevant AI actions through the SDK.

Public API:
    ComplianceReport    - structured report model
    ReportSection       - individual section within a report
    ReportGenerator     - generates Article 12 evidence reports from stored data
"""
from .models import ComplianceReport, ReportSection
from .report import ReportGenerator

__all__ = [
    "ComplianceReport",
    "ReportGenerator",
    "ReportSection",
]
