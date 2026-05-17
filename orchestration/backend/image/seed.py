#  Hekate Orchestration - Text Seed Utilities
#
#  Human-readable seeds hashed to ComfyUI-compatible ints.
#  Ported from noz-ai.
#
#  Depends on: (none)
#  Used by:    image/comfy/blocks/__init__.py
from __future__ import annotations

import hashlib
import random
import struct

_ADJECTIVES = [
    "ancient", "bitter", "bold", "brave", "bright", "broad", "calm", "clean",
    "clever", "cold", "cool", "crisp", "dark", "deep", "eager", "early",
    "fair", "fast", "fierce", "fine", "firm", "flat", "fond", "free",
    "fresh", "full", "gentle", "glad", "golden", "grand", "grave", "great",
    "green", "grey", "grim", "happy", "hard", "harsh", "heavy", "hidden",
    "high", "hollow", "holy", "honest", "humble", "hungry", "idle", "keen",
    "kind", "lame", "large", "last", "late", "lean", "light", "little",
    "lofty", "lone", "long", "lost", "loud", "low", "lucky", "mad",
    "magic", "mean", "merry", "mighty", "mild", "misty", "modest", "moist",
    "narrow", "neat", "noble", "odd", "old", "open", "pale", "patient",
    "plain", "poor", "proud", "pure", "quick", "quiet", "rapid", "rare",
    "raw", "ready", "real", "red", "rich", "rigid", "ripe", "rough",
    "round", "royal", "rude", "rustic", "sad", "safe", "sandy", "secret",
    "sharp", "short", "shy", "silent", "silver", "simple", "sleek", "slim",
    "slow", "small", "smooth", "snowy", "soft", "solid", "sour", "spare",
    "stark", "steady", "steep", "stiff", "still", "stout", "strange", "strong",
    "subtle", "sudden", "sunny", "super", "sure", "sweet", "swift", "tall",
    "tame", "tender", "thick", "thin", "tight", "tired", "tough", "true",
    "twin", "vague", "vast", "vivid", "warm", "weak", "weary", "weird",
    "wet", "white", "whole", "wide", "wild", "wise", "young", "zealous",
]

_NOUNS = [
    "arch", "arrow", "basin", "beacon", "blade", "bloom", "bluff", "bone",
    "branch", "bridge", "brook", "cairn", "candle", "canyon", "cape", "castle",
    "cave", "chain", "chalk", "chime", "cinder", "cliff", "cloud", "coast",
    "comet", "coral", "corner", "crest", "cross", "crown", "crystal", "dagger",
    "dawn", "desert", "diamond", "dome", "door", "dragon", "dream", "drift",
    "drum", "dune", "dust", "eagle", "earth", "echo", "edge", "elm",
    "ember", "falcon", "fern", "field", "fire", "flame", "flint", "flood",
    "flower", "forest", "forge", "fort", "fossil", "frost", "garden", "gate",
    "gem", "glacier", "glen", "gorge", "grain", "grove", "guard", "gust",
    "hammer", "harbor", "hawk", "hearth", "hedge", "helm", "heron", "hill",
    "hollow", "horn", "horse", "hound", "island", "ivy", "jade", "jewel",
    "jungle", "keep", "kernel", "kettle", "knoll", "lake", "lance", "lantern",
    "lark", "lava", "leaf", "ledge", "lily", "lion", "lotus", "lynx",
    "marsh", "mask", "meadow", "mesa", "mill", "mirror", "mist", "moon",
    "moss", "moth", "mound", "nest", "night", "oak", "oasis", "ocean",
    "orchid", "otter", "owl", "palm", "panther", "pass", "path", "peak",
    "pearl", "pebble", "pine", "plume", "pond", "portal", "prism", "quail",
    "quarry", "rain", "raven", "reef", "ridge", "ring", "river", "robin",
    "rock", "root", "rose", "ruin", "sage", "sand", "seal", "seed",
    "shade", "shell", "shield", "shore", "shrine", "sierra", "silk", "silver",
    "sky", "slate", "slope", "smoke", "snake", "snow", "song", "spark",
    "spear", "sphinx", "spider", "spirit", "spring", "spruce", "star", "steam",
    "stone", "storm", "stream", "summit", "swan", "sword", "temple", "thorn",
    "throne", "thunder", "tide", "tiger", "timber", "torch", "tower", "trail",
    "tree", "trench", "tulip", "vale", "valley", "vault", "vine", "violet",
    "viper", "void", "wave", "whale", "willow", "wind", "wing", "wolf",
    "wraith", "wren", "zenith",
]


def hash_seed(text: str) -> int:
    if not text:
        return 0
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    value = struct.unpack("<q", digest[:8])[0]
    return (value & 0x3FFFFFFFFFFFF) | 1


def random_seed() -> str:
    adj = random.choice(_ADJECTIVES)
    noun = random.choice(_NOUNS)
    return f"{adj}-{noun}"
