"""Map an SMT feeder `part_value` to an InvenTree Part and reconcile its stock.

Feeder values are either a generic part name (e.g. 'C-100nF-10%-50V-0603-X7R') that matches
Part.name, or a manufacturer part number (e.g. 'CSD17578Q5AT') matching ManufacturerPart.MPN,
or occasionally an IPN. Matching is exact; unmatched values are reported for manual review
(they're usually placeholder feeder entries like 'R-0R-x%-0402-xmW' or parts not in the library).
"""


def build_lookups():
    """Return (by_name, by_mpn, by_ipn) dicts -> Part, prefetched from the ORM."""
    from part.models import Part
    from company.models import ManufacturerPart

    by_name, by_ipn = {}, {}
    for p in Part.objects.filter(active=True).only("id", "name", "IPN"):
        by_name.setdefault(p.name, p)
        if p.IPN:
            by_ipn.setdefault(p.IPN, p)
    by_mpn = {}
    for mp in ManufacturerPart.objects.select_related("part").only("MPN", "part"):
        if mp.MPN and mp.part_id:
            by_mpn.setdefault(mp.MPN, mp.part)
    return by_name, by_mpn, by_ipn


def resolve(value, lookups):
    by_name, by_mpn, by_ipn = lookups
    v = (value or "").strip()
    if not v:
        return None
    return by_name.get(v) or by_mpn.get(v) or by_ipn.get(v)


def reconcile_stock(part, quantity, location, user=None):
    """Set the part's stock AT `location` to `quantity` (create/adjust a single StockItem).

    Only the SMT-location stock is managed here; stock elsewhere is untouched, so the part's
    total stock = SMT feeders + other locations. Returns 'created' | 'updated' | 'unchanged'.
    """
    from stock.models import StockItem

    qty = int(round(float(quantity)))
    si = StockItem.objects.filter(part=part, location=location).order_by("-quantity").first()
    if si is None:
        if qty <= 0:
            return "unchanged"
        StockItem.objects.create(part=part, location=location, quantity=qty)
        return "created"
    if int(si.quantity) == qty:
        return "unchanged"
    # count-style adjustment so InvenTree records a tracking entry
    try:
        si.stocktake(qty, user, notes="SMT feeder sync")
    except Exception:
        si.quantity = qty
        si.save()
    return "updated"
