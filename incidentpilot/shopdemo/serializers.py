"""Order serialization. ``serialize_order_v2`` is the buggy refactor shipped by the
``bad_deploy`` fault: the bulk discount stays a ``Decimal``, which JSON can't encode.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def serialize_order_v1(order: dict[str, Any]) -> dict[str, Any]:
    return {**order, "total": str(order["total"])}


def serialize_order_v2(order: dict[str, Any]) -> dict[str, Any]:
    out = dict(order)
    if order["quantity"] > 1:
        out["discount"] = (order["total"] * Decimal("0.05")).quantize(Decimal("0.01"))
        out["total"] = str(order["total"] - out["discount"])
    else:
        out["total"] = str(order["total"])
    json.dumps(out)  # validate the payload before returning it
    return out
