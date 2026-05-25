"""Track and car name normalisation.

ACC reports internal codes in its static shared-memory page (``spa``,
``mclaren_720s_gt3``); iRacing (v0.2) reports human names. We normalise both to
display strings here so the MCP tools can speak the same language a driver does.
Unknown codes fall back to a humanised form rather than raising -- a new car or
track in a game update should never crash ingest.
"""

from __future__ import annotations

# ACC internal track code -> display name. Not exhaustive; extend as DLC lands.
TRACK_NAMES: dict[str, str] = {
    "barcelona": "Barcelona-Catalunya",
    "brands_hatch": "Brands Hatch",
    "cota": "Circuit of the Americas",
    "donington": "Donington Park",
    "hungaroring": "Hungaroring",
    "imola": "Imola",
    "indianapolis": "Indianapolis",
    "kyalami": "Kyalami",
    "laguna_seca": "Laguna Seca",
    "misano": "Misano",
    "monza": "Monza",
    "mount_panorama": "Mount Panorama",
    "nurburgring": "Nurburgring GP",
    "nurburgring_24h": "Nurburgring 24h",
    "oulton_park": "Oulton Park",
    "paul_ricard": "Paul Ricard",
    "red_bull_ring": "Red Bull Ring",
    "silverstone": "Silverstone",
    "snetterton": "Snetterton",
    "spa": "Spa-Francorchamps",
    "suzuka": "Suzuka",
    "valencia": "Valencia (Ricardo Tormo)",
    "watkins_glen": "Watkins Glen",
    "zandvoort": "Zandvoort",
    "zolder": "Zolder",
}

# ACC internal track code -> on-track length in metres. Used ONLY as a fallback
# when ACC's shared-memory ``trackSPlineLength`` reports 0 (it does, even in clean
# sessions) -- a valid in-game value always wins (see acc/mapping.resolve_track_length).
# These are the published circuit lengths for the layout ACC ships; ``nurburgring``
# was cross-checked (~5152 m) against the committed fixture by integrating speed
# over the final straight. The rest are accurate to ~1%; confirm a track against a
# clean in-game readout before trusting its distances to the metre. An unknown code
# returns None and distances fall back to the normalised 0..1 key rather than guess.
TRACK_LENGTHS: dict[str, float] = {
    "barcelona": 4655.0,
    "brands_hatch": 3916.0,        # GP circuit
    "cota": 5513.0,
    "donington": 4020.0,           # GP circuit
    "hungaroring": 4381.0,
    "imola": 4909.0,
    "indianapolis": 3925.0,        # IMS road course (verify layout)
    "kyalami": 4522.0,
    "laguna_seca": 3602.0,
    "misano": 4226.0,
    "monza": 5793.0,
    "mount_panorama": 6213.0,
    "nurburgring": 5148.0,         # GP-Strecke; ~5152 m measured from the fixture
    "nurburgring_24h": 25378.0,    # GP + Nordschleife (24h layout)
    "oulton_park": 4307.0,         # International circuit
    "paul_ricard": 5842.0,         # GP/1C layout
    "red_bull_ring": 4318.0,
    "silverstone": 5891.0,         # GP circuit
    "snetterton": 4779.0,          # 300 circuit
    "spa": 7004.0,
    "suzuka": 5807.0,
    "valencia": 4005.0,            # Circuit Ricardo Tormo
    "watkins_glen": 5552.0,        # long course (the Boot)
    "zandvoort": 4259.0,
    "zolder": 4011.0,
}

# ACC internal car-model code -> display name.
CAR_NAMES: dict[str, str] = {
    "amr_v8_vantage_gt3": "Aston Martin V8 Vantage GT3",
    "audi_r8_lms": "Audi R8 LMS",
    "audi_r8_lms_evo": "Audi R8 LMS Evo",
    "audi_r8_lms_evo_ii": "Audi R8 LMS Evo II",
    "bentley_continental_gt3_2016": "Bentley Continental GT3 (2016)",
    "bentley_continental_gt3_2018": "Bentley Continental GT3 (2018)",
    "bmw_m4_gt3": "BMW M4 GT3",
    "bmw_m6_gt3": "BMW M6 GT3",
    "ferrari_296_gt3": "Ferrari 296 GT3",
    "ferrari_488_gt3": "Ferrari 488 GT3",
    "ferrari_488_gt3_evo": "Ferrari 488 GT3 Evo",
    "ford_mustang_gt3": "Ford Mustang GT3",
    "honda_nsx_gt3": "Honda NSX GT3",
    "honda_nsx_gt3_evo": "Honda NSX GT3 Evo",
    "jaguar_g3": "Emil Frey Jaguar G3",
    "lamborghini_huracan_gt3": "Lamborghini Huracan GT3",
    "lamborghini_huracan_gt3_evo": "Lamborghini Huracan GT3 Evo",
    "lamborghini_huracan_gt3_evo2": "Lamborghini Huracan GT3 Evo2",
    "lexus_rc_f_gt3": "Lexus RC F GT3",
    "mclaren_650s_gt3": "McLaren 650S GT3",
    "mclaren_720s_gt3": "McLaren 720S GT3",
    "mclaren_720s_gt3_evo": "McLaren 720S GT3 Evo",
    "mercedes_amg_gt3": "Mercedes-AMG GT3",
    "mercedes_amg_gt3_evo": "Mercedes-AMG GT3 Evo",
    "nissan_gt_r_gt3_2017": "Nissan GT-R Nismo GT3 (2017)",
    "nissan_gt_r_gt3_2018": "Nissan GT-R Nismo GT3 (2018)",
    "porsche_991_gt3_r": "Porsche 911 GT3 R (991)",
    "porsche_991ii_gt3_r": "Porsche 911 II GT3 R (991)",
    "porsche_992_gt3_r": "Porsche 911 GT3 R (992)",
}


def _humanise(code: str) -> str:
    """Best-effort display name for an unmapped code: ``some_new_track`` -> ``Some New Track``."""
    return code.replace("_", " ").strip().title()


def track_name(code: str | None) -> str:
    """Display name for an ACC track code, falling back to a humanised form."""
    if not code:
        return "Unknown"
    return TRACK_NAMES.get(code, _humanise(code))


def car_name(code: str | None) -> str:
    """Display name for an ACC car-model code, falling back to a humanised form."""
    if not code:
        return "Unknown"
    return CAR_NAMES.get(code, _humanise(code))


def track_length_m(code: str | None) -> float | None:
    """Fallback on-track length (metres) for an ACC track code, or None if unknown.

    Used only when ACC's shared-memory ``trackSPlineLength`` is 0/invalid; a valid
    in-game value always wins. Unknown codes return None so distances fall back to
    the normalised 0..1 key rather than guessing a length.
    """
    if not code:
        return None
    return TRACK_LENGTHS.get(code)
