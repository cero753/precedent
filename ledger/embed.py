"""Tiny deterministic hashing embedder. Stand-in for a real embedding model (e.g. Voyage + pgvector)."""
import hashlib
import math
import re

DIM = 128


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    toks = re.findall(r"[a-z0-9]+", text.lower())
    for t in toks + [a + "_" + b for a, b in zip(toks, toks[1:])]:
        h = int(hashlib.md5(t.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 8) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / n, 5) for x in v]


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))
