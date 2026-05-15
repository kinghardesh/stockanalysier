from app.risk.decision import Rejected, RiskDecision


def persist_decision(db, proposal_row, decision: RiskDecision) -> None:
    """Reflect a RiskDecision back onto the trade_proposals row.

    The risk engine stays ORM-free; this helper bridges the decision back
    to the proposal row so downstream analysis can filter on rejected_reason.
    """
    if isinstance(decision, Rejected):
        proposal_row.rejected_reason = decision.reason
        db.commit()
