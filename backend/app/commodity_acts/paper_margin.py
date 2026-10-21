"""Optional read-only margin capability. No login, order API or raw-body logging.

Official field mapping: Kotak-Neo/Kotak-Neo docs/portfolio-and-funds-apis/margins.md
ordMrgn = margin required for the order, distinct from existing-account margin.
An estimate is never relabeled as actual blocked funds.
"""
from app.commodity_acts.broker import number
from app.commodity_acts.paper_naked import SingleMarginEvidence


class KotakSingleMarginReader:
    __slots__ = ("__read", "__clock")

    def __init__(self, read_margin, clock):
        self.__read, self.__clock = read_margin, clock

    def estimate(self, snapshot, candidate, policy):
        try:
            response = self.__read(exchange_segment="mcx_fo", price=str(candidate["executable_credit"]),
                order_type="L", product="NRML", quantity=str(policy.evidence.lot_size),
                instrument_token=candidate["short_leg"]["instrument_token"], transaction_type="S")
            data = response.get("data") if isinstance(response, dict) else None
            if (not isinstance(data, dict) or data.get("stat") != "Ok" or data.get("stCode") not in (200, "200")
                    or data.get("rmsVldtd") != "OK"):
                return None
            required = number(data.get("ordMrgn"), positive=True)
            if required is None:
                return None
            return SingleMarginEvidence(instrument_id=snapshot.instrument_id,
                short_token=candidate["short_leg"]["instrument_token"], hedge_token=None,
                expiry=policy.evidence.option_expiry, lot_size=policy.evidence.lot_size,
                required_margin=required, observed_at=self.__clock(),
                source="KOTAK_NEO_3_0_7_MARGIN_REQUIRED_ORDMRGN_SINGLE_SELL_NRML_ESTIMATE")
        except Exception:
            return None  # Never emit SDK errors, account balances, IDs or credentials.


def authorized_margin_reader(sdk, clock):
    """Explicitly supplied existing authorized session only; never creates one.

    The returned capability has no order methods and is not constructed by the
    default consumer-key-only commodity worker. No credentials are persisted.
    """
    config = getattr(sdk, "configuration", None)
    if not config or not config.edit_token or not config.edit_sid:
        return None
    return KotakSingleMarginReader(sdk.margin_required, clock)
