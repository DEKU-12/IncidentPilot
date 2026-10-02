"""ShopDemo: a small three-service shop that IncidentPilot investigates.

frontend -> orders -> payments. Each service writes structured JSON logs and
periodic metric points, and exposes admin endpoints so the chaos controller can
break it in known ways.
"""
