#!/usr/bin/env python3
"""
The Sunday recap, written by Claude.

build_data.py assembles the facts and a plain-prose fallback. This module
hands the facts to Claude and asks for something worth reading, then checks
every number in the answer against the facts before letting it through.

Any failure returns None and the caller publishes the fallback: no key, no
network, a rate limit, malformed output, too many paragraphs, or a number
that does not appear in the data. On a page whose whole point is honest
self-grading, a fabricated stat is the one unacceptable outcome, so the
check is mechanical rather than a matter of trusting the prompt.
"""

import json
import os
import re
import sys

MODEL = "claude-opus-5"
EFFORT = "medium"          # four paragraphs does not need deep reasoning
MAX_TOKENS = 8000          # room for adaptive thinking plus the prose
MAX_PARAGRAPHS = 4

SYSTEM = """You write the Sunday recap for CFB Edge Finder, a college football \
betting model that publishes its own results. You are the model's beat writer, \
and you do not work for its PR department.

VOICE
Opinionated, irreverent, dry, funny. Sarcastic when the model has earned \
sarcasm, genuinely boastful when it has earned that instead, and merciless \
when it embarrassed itself. The model is the protagonist and the only \
acceptable target. Never make a joke at the expense of a player, a coach, a \
team, or a fan base; punch at the model, at the betting market, or at the \
whole idea of predicting football. Land at least one real laugh. Do not be \
zany and do not try too hard. The comedy comes from being blunt about numbers \
that other sites would bury.

STRUCTURE
Between two and four paragraphs. Four is a ceiling, not a target: if three \
paragraphs say it, write three. Each paragraph runs 50 to 90 words. Prose \
only, no lists, no headings, no bullets. One arc across the whole piece: what \
the model played this week and how that went, the one or two calls actually \
worth talking about, and where it leaves the season.

The headline is a single line under twelve words that states the result with \
some attitude. It is not a stat line.

FACTS
Every number you write must appear in the DATA block. Do not estimate, \
re-round, average, extrapolate, or invent a figure. If a point you want to \
make needs a number DATA does not contain, make a different point. Team \
names, picks and final scores come from DATA too. When DATA says the week was \
graded in hindsight, say so and be rude about how little it counts.

NEVER
- Predict anything, preview next week, or give betting advice.
- Explain the model's methodology. The reader is looking at the table.
- Write a responsible-gambling disclaimer. The footer has one.
- Use an em dash or an en dash. Commas and periods only.
- Open with the bare record, or with "Let's be honest", "Look,", "Well,", \
"Folks", or a rhetorical question.
- Invent a category or a split that DATA does not contain. There is no line in DATA for road underdogs, ranked opponents, primetime games, conference play, or favorites versus dogs, so you do not know any of those things. You know the week, the three markets, and the individual plays.
- Reuse the opening move, the structure, or the jokes from the RECENT block."""

SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "paragraphs": {
                # structured outputs reject minItems above 1 and maxItems
                # entirely, so the two-to-four bound lives in the prompt and
                # the code trims to MAX_PARAGRAPHS
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["headline", "paragraphs"],
        "additionalProperties": False,
    },
}

LAST_ERROR = None            # why the most recent attempt fell back, for the store

def _fail(reason):
    global LAST_ERROR
    LAST_ERROR = reason
    print(f"recap: builtin ({reason})", file=sys.stderr)
    return None


NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
DASHES = re.compile(r"\s*[—–]\s*")


# --- the number guard --------------------------------------------------------

def _add(vals, v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return
    if v != v or v in (float("inf"), float("-inf")):
        return
    for x in (v, -v, abs(v), round(v, 1), round(v, 2)):
        vals.add(round(float(x), 4))
    if 0.0 <= v <= 1.0:                      # a rate quoted as a percentage
        for x in (v * 100, round(v * 100), round(v * 100, 1)):
            vals.add(round(float(x), 4))


def allowed_numbers(facts):
    """Every number the recap is permitted to contain.

    The facts tree, each value also in the forms prose would naturally use
    (rounded, negated, as a percentage), plus the digits in any string such
    as a record or a final score. Small integers are always allowed because
    ordinary sentences count things.
    """
    vals = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            _add(vals, node)
        elif isinstance(node, str):
            for m in NUM_RE.finditer(node):
                _add(vals, m.group().replace(",", ""))

    walk(facts)
    for i in range(0, 11):        # ordinary sentences count things
        _add(vals, i)
    return vals


def first_bad_number(text, allowed, tol=0.051):
    for m in NUM_RE.finditer(text):
        try:
            v = float(m.group().replace(",", ""))
        except ValueError:
            continue
        if not any(abs(v - a) <= tol for a in allowed):
            return m.group()
    return None


# --- the call ----------------------------------------------------------------

def _prompt(facts, recent):
    parts = []
    if recent:
        parts.append("RECENT (your last few openings, do not echo them)\n"
                     + json.dumps(recent, indent=1))
    parts.append("DATA\n" + json.dumps(facts, indent=1, sort_keys=True))
    parts.append("Write this week's recap.")
    return "\n\n".join(parts)


def write_recap(facts, recent=None):
    """Returns {"headline", "paragraphs", "source"} or None."""
    global LAST_ERROR
    LAST_ERROR = None
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return _fail("no ANTHROPIC_API_KEY")
    try:
        import anthropic
    except ImportError:
        return _fail("anthropic package not installed")

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            output_config={"effort": EFFORT, "format": SCHEMA},
            messages=[{"role": "user", "content": _prompt(facts, recent)}],
        )
    except Exception as e:                      # noqa: BLE001 - never fail the build
        return _fail(f"{type(e).__name__}: {e}")

    if response.stop_reason == "max_tokens":
        return _fail("hit max_tokens")
    if response.stop_reason == "refusal":
        return _fail("refused")

    try:
        text = next(b.text for b in response.content if b.type == "text")
        out = json.loads(text)
        headline = str(out["headline"]).strip()
        paragraphs = [str(p).strip() for p in out["paragraphs"] if str(p).strip()]
    except (StopIteration, KeyError, TypeError, ValueError) as e:
        return _fail(f"unreadable output: {e}")

    if not headline or not paragraphs:
        return _fail("empty output")
    paragraphs = paragraphs[:MAX_PARAGRAPHS]

    # commas instead of dashes, belt and braces over the instruction
    headline = DASHES.sub(", ", headline)
    paragraphs = [DASHES.sub(", ", p) for p in paragraphs]

    allowed = allowed_numbers(facts)
    for chunk in [headline] + paragraphs:
        bad = first_bad_number(chunk, allowed)
        if bad:
            return _fail(f"invented the number {bad}")

    usage = getattr(response, "usage", None)
    if usage:
        print(f"recap: claude ({usage.input_tokens} in, {usage.output_tokens} out)",
              file=sys.stderr)
    else:
        print("recap: claude", file=sys.stderr)
    return {"headline": headline, "paragraphs": paragraphs, "source": "claude"}
