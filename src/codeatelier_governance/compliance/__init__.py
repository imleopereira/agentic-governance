"""EU AI Act compliance report generation from audit trail data.

Public API:
    ComplianceReport    - structured report model
    ReportSection       - individual section within a report
    ReportGenerator     - generates compliance reports from stored data
"""
from .models import ComplianceReport, ReportSection
from .report import ReportGenerator

__all__ = [
    "ComplianceReport",
    "ReportGenerator",
    "ReportSection",
]
