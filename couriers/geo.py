"""Geo helpers for the courier layer.

Currently just the great-circle distance used to turn a GPS trail
(``LocationTrailPoint`` rows) into the ``distanceKm`` the stats screen shows.
"""
import math

EARTH_RADIUS_KM = 6371.0

# Two consecutive fixes farther apart than this are treated as a GPS jump
# (tunnel/teleport/bad fix) and skipped, so one wild reading can't inflate the
# shift distance by hundreds of km.
_MAX_SEGMENT_KM = 5.0


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance between two lat/lng points, in kilometres."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2)
    return EARTH_RADIUS_KM * 2 * math.asin(min(1.0, math.sqrt(a)))


def trail_distance_km(points):
    """Sum the distance along an ordered iterable of objects with ``lat``/``lng``.

    Segments longer than ``_MAX_SEGMENT_KM`` are dropped as GPS noise. Returns a
    float (kilometres); 0.0 for fewer than two points.
    """
    total = 0.0
    prev = None
    for p in points:
        if prev is not None:
            d = haversine_km(prev.lat, prev.lng, p.lat, p.lng)
            if d <= _MAX_SEGMENT_KM:
                total += d
        prev = p
    return total
