"""Mechanic and theme tags from a game's name and description.

Roblox genres are coarse and often unset, so these keyword tags group games by what players
actually do or what the game is about. A game can have several tags. Rough by design: good for
comparing groups of games, not for judging any single game.
"""

import re

TAGS = [
    ("obby", r"\bobby|parkour|obstacle course|\btower of\b"),
    ("tycoon", r"tycoon"),
    ("simulator", r"simulator|\bsim\b"),
    ("steal-a", r"\bsteal (a|an|the)\b|\bsteal\b"),
    ("grow-a", r"\bgrow (a|an|your)\b"),
    ("+1 / evolution", r"\+ ?1\b|evolution|evolve"),
    ("ragdoll / physics", r"ragdoll|physics"),
    ("horror", r"horror|scary|creepy|backrooms|anomal|\bdon'?t\b|\bdont\b|nightmare|haunted"),
    ("rng / gacha", r"\brng\b|\baura\b|gacha|\bspin\b|lucky block"),
    ("tower defense", r"tower defen[cs]e|\btd\b"),
    ("anime / IP", r"anime|naruto|sukuna|gojo|jjk|jujutsu|one piece|dragon ball|demon slayer|bleach|pokemon|"
                   r"marvel|spider[- ]?man|sonic|minecraft|fnaf|freddy|squid game|mario"),
    ("brainrot / meme", r"brainrot|skibidi|sahur|tralalero|italian|67\b|six seven|sigma|meme"),
    ("pets / hatching", r"\bpets?\b|hatch|\beggs?\b|creature|monster"),
    ("fighting / pvp", r"\bfight|battle|\bpvp\b|combat|boxing|\bmma\b|brawl|sword|duel"),
    ("shooter", r"shoot|\bgun|sniper|\bfps\b|rivals"),
    ("vehicles / racing", r"\bcars?\b|\brace\b|racing|\bdriv|\btruck|\bbike|\bboat|plane|\btrain\b"),
    ("roleplay / life", r"roleplay|\brp\b|\blife\b|\btown\b|\bcity\b|\bschool\b|\bhouse\b|family|brookhaven"),
    ("clicker / idle", r"click|\bidle\b|incremental|afk"),
    ("escape / survive", r"escape|run from|surviv|\bflee\b|tsunami|flood|disaster|\blava\b"),
    ("fishing", r"\bfish"),
    ("mining / digging", r"\bmin(e|ing)\b|\bdig\b|digging|\bore\b"),
    ("cooking / shop", r"\bcook|restaurant|\bcafe\b|\bfood\b|bakery|pizza|\bshop\b|store|business|market"),
    ("co-op / duo", r"2 ?players?|\bduo\b|co-?op|\bteam\b|together|with friends"),
    ("troll / twist", r"\btroll|\bbut\b|impossible|noob vs|\bnoob\b"),
    ("quiz / puzzle", r"puzzle|\bquiz|guess|trivia|\bmath\b|riddle"),
    ("sports", r"soccer|football|basketball|baseball|volleyball|\bgolf|tennis|skate"),
    ("hide & seek / tag", r"hide and seek|hide & seek|\bhide\b|\bseek\b|\btag\b|murder|mystery"),
    ("building / sandbox", r"\bbuild|sandbox|\bcraft"),
    ("music / dance", r"music|\bdance|rhythm|\bsing"),
    ("collect / catch", r"\bcatch|collect|\bfind (the|all)\b|\bhunt"),
    ("sled / snow / xmas", r"\bsled|\bsnow|christmas|xmas|winter|santa"),
]
_COMPILED = [(name, re.compile(rx, re.I)) for name, rx in TAGS]


def tags(name, description=""):
    """Tags from the name, plus tags from the first 300 characters of the description."""
    text = f"{name or ''} \n {(description or '')[:300]}"
    return [n for n, rx in _COMPILED if rx.search(text)]
