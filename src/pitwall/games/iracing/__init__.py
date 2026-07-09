"""iRacing telemetry reader.

A near-verbatim port of the ``.ibt`` decode layer from the racing-telemetry-visualiser
project (``rtv.catalog.types`` / ``rtv.ingest.ibt`` / ``rtv.ingest.normalize``), plus a
thin pitwall adapter (:mod:`mapping`, :mod:`reader`) that maps the decoded iRacing
variables onto pitwall's canonical channel schema. Keeping the decode layer a faithful
copy means the two projects reconcile trivially when pitwall is folded into the visualiser.
"""
