"""HTC (Horry Telephone Cooperative) cable: the Horry/Marion residential
lineup, and which channel a network name lands on.

Source: htcinc.net/video/channel-lineup (residential, copied 2026-10-27).
Numbers can change without notice; this is the one place to fix them.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# (number, name, tier) — tier "Basic" or "Plus"
LINEUP = [
    (3, "Headline News", "Basic"), (5, "Freeform", "Plus"), (6, "C-SPAN", "Basic"), (7, "FOX", "Basic"),
    (8, "PBS", "Basic"), (10, "NBC", "Basic"), (11, "QVC", "Basic"), (13, "CBS", "Basic"), (15, "ABC", "Basic"),
    (16, "CW", "Basic"), (18, "HSN", "Basic"), (20, "COZI", "Basic"), (22, "TBN", "Basic"),
    (23, "Lifetime", "Plus"), (24, "HGTV", "Plus"), (25, "TNT", "Plus"), (26, "CNN", "Plus"), (27, "USA", "Plus"),
    (28, "ESPN", "Plus"), (29, "ESPN2", "Plus"), (30, "Lifetime Movies", "Plus"), (31, "Discovery", "Plus"),
    (32, "VH1", "Plus"), (33, "BET", "Plus"), (34, "Comedy Central", "Plus"), (35, "MTV", "Plus"),
    (36, "Weather Channel", "Plus"), (37, "Disney Channel", "Plus"), (38, "TLC", "Plus"), (39, "ION", "Plus"),
    (40, "Syfy", "Plus"), (41, "Cartoon Network", "Plus"), (42, "CNBC", "Plus"), (43, "A&E", "Plus"),
    (44, "CMT", "Plus"), (45, "TCM", "Plus"), (46, "AMC", "Plus"), (47, "TBS", "Plus"), (50, "E!", "Plus"),
    (51, "History", "Plus"), (52, "Golf Channel", "Plus"), (53, "Nickelodeon", "Plus"), (54, "TV Land", "Plus"),
    (55, "FS1", "Plus"), (56, "truTV", "Plus"), (57, "Animal Planet", "Plus"), (58, "Travel Channel", "Plus"),
    (59, "OWN", "Plus"), (60, "FX", "Plus"), (61, "FOX News", "Plus"), (62, "Food Network", "Plus"),
    (63, "Paramount Network", "Plus"), (64, "National Geographic", "Plus"), (65, "Bravo", "Plus"),
    (66, "FOX Business", "Plus"), (67, "ACC Network", "Plus"), (68, "NewsNation", "Basic"), (70, "MS NOW", "Plus"),
    (71, "ID", "Plus"), (72, "FXX", "Plus"), (73, "SEC Network", "Plus"), (74, "BravesVision", "Plus"),
    (75, "FOX Weather", "Plus"), (76, "Tennis Channel", "Plus"), (77, "Oxygen", "Plus"), (78, "Hallmark", "Plus"),
    (79, "Hallmark Mystery", "Plus"), (81, "WE tv", "Plus"), (82, "Palmetto Sports", "Plus"), (83, "BBC America", "Plus"),
    (84, "Laff", "Basic"), (86, "PBS Kids", "Basic"), (88, "Comet", "Basic"), (89, "MyNetwork", "Basic"),
    (90, "Start TV", "Basic"), (91, "MeTV", "Basic"), (92, "Bounce", "Basic"), (94, "Grit", "Basic"),
    (96, "CBS Sports Network", "Plus"), (97, "MLB Network", "Plus"), (125, "True Crime", "Basic"),
    (128, "Telemundo", "Basic"), (133, "Dabl", "Basic"), (136, "POP", "Plus"),
    (4, "HTC Local Access", "Basic"), (9, "City Gov. Access", "Basic"), (12, "Educational Access", "Basic"),
    (14, "County Gov. Access", "Basic"), (17, "GSTV", "Basic"), (19, "QVC2", "Basic"), (85, "ION Mystery", "Basic"),
    (87, "The Nest", "Basic"), (93, "WPDE2", "Basic"), (98, "Beach TV", "Basic"), (99, "Jewelry TV", "Basic"),
    (100, "ROAR", "Basic"), (101, "ETV World", "Basic"), (102, "South Carolina Channel", "Basic"), (129, "EWTN", "Basic"),
]
LINEUP.sort(key=lambda c: c[0])

# How ESPN / TVmaze spell a network -> HTC's number. Lowercased, punctuation-light.
ALIASES: Dict[str, int] = {
    "espn": 28, "espn2": 29, "fs1": 55, "fox sports 1": 55, "acc network": 67, "accn": 67, "acc network extra": 0,
    "sec network": 73, "secn": 73, "cbs sports network": 96, "cbssn": 96, "mlb network": 97, "mlbn": 97,
    "golf channel": 52, "golf": 52, "tennis channel": 76, "tnt": 25, "tbs": 47, "trutv": 56, "tru tv": 56, "usa": 27,
    "usa network": 27, "nbc": 10, "cbs": 13, "fox": 7, "abc": 15, "the cw": 16, "cw": 16, "pbs": 8, "ion": 39,
    "ion television": 39, "freeform": 5, "fx": 60, "fxx": 72, "amc": 46, "a&e": 43, "hgtv": 24, "food network": 62,
    "discovery": 31, "discovery channel": 31, "tlc": 38, "history": 51, "bravo": 65, "e!": 50, "syfy": 40,
    "lifetime": 23, "national geographic": 64, "nat geo": 64, "comedy central": 34, "mtv": 35, "vh1": 32, "bet": 33,
    "cnn": 26, "fox news": 61, "fox news channel": 61, "cnbc": 42, "ms now": 70, "msnbc": 70, "hallmark": 78,
    "hallmark channel": 78, "investigation discovery": 71, "id": 71, "oxygen": 77, "bbc america": 83, "tcm": 45,
    "turner classic movies": 45, "paramount network": 63, "nickelodeon": 53, "cartoon network": 41,
    "disney channel": 37, "own": 59, "we tv": 81, "telemundo": 128, "travel channel": 58, "animal planet": 57,
    "the weather channel": 36, "cmt": 44, "tv land": 54, "pop": 136, "bravesvision": 74,
    "newsnation": 68, "fox business network": 66, "fox business": 66, "fox weather": 75, "hln": 3, "headline news": 3,
    "c-span": 6, "lifetime movie network": 30, "lmn": 30, "hallmark mystery": 79, "hallmark movies & mysteries": 79,
    "ion mystery": 0, "metv": 91, "me tv": 91, "cozi tv": 20, "start tv": 90, "bounce tv": 92, "grit": 94, "comet": 88,
    "laff": 84, "dabl": 133, "true crime network": 125, "mynetworktv": 89, "pbs kids": 86, "disney": 37,
    "nick": 53, "cartoon network/adult swim": 41, "adult swim": 41, "wetv": 81, "tennis": 76, "nfl network": 0,
}
# What kind of channel it is, for the guide's filters.
CATEGORY = {}
for _n in (28, 29, 55, 67, 73, 52, 96, 97, 76, 74, 82):
    CATEGORY[_n] = "Sports"
for _n in (3, 6, 26, 42, 61, 66, 68, 70, 75, 36):
    CATEGORY[_n] = "News"
for _n in (7, 8, 10, 13, 15, 16, 89, 128):
    CATEGORY[_n] = "Local"
for _n in (37, 41, 53, 86):
    CATEGORY[_n] = "Kids"
for _n in (45, 30, 78, 79, 46, 90, 133):
    CATEGORY[_n] = "Movies"
for _n in (11, 18, 22, 125, 20, 84, 88, 91, 92, 94):
    CATEGORY.setdefault(_n, "Classic & more")
NAMES = {n: name for n, name, _ in LINEUP}
# Quick-tune favourites for the remote: sports first, then the locals.
FAVORITES = [28, 29, 55, 67, 73, 52, 96, 97, 25, 47, 27, 10, 13, 7, 15]


def channel_for(network: Optional[str]) -> Optional[int]:
    key = re.sub(r"\s+", " ", (network or "").strip().lower())
    n = ALIASES.get(key)
    return n or None


def channels_for(networks: List[str]) -> List[Dict[str, object]]:
    out, seen = [], set()
    for net in networks or []:
        n = channel_for(net)
        if n and n not in seen:
            seen.add(n)
            out.append({"ch": n, "name": NAMES.get(n, net), "net": net})
    return out


# Remote-sized names for the quick-tune grid.
SHORT = {67: "ACCN", 73: "SECN", 52: "Golf", 96: "CBSSN", 97: "MLBN", 55: "FS1"}


def favorites() -> List[Dict[str, object]]:
    return [{"ch": n, "name": SHORT.get(n, NAMES[n]), "full": NAMES[n]} for n in FAVORITES]
