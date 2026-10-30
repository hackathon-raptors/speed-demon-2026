"""S and D for one team-track, behind the one function the board calls.

S is code lines counted by scc after the language's formatter, over the repo at
the recorded commit; D is 1 + the direct non-stdlib runtime dependencies in the
manifest. Both come from harness/static_axes.py.
"""
import importlib
import logging
import os

log = logging.getLogger("sdboard")


_warned = set()


def _warn_once(message):
    if message not in _warned:
        _warned.add(message)
        log.warning(message)


def _module():
    try:
        return importlib.import_module("static_axes")   # harness/ is on sys.path
    except ImportError:
        return None


# scc names folded into the language a reader knows; Dockerfile is build glue every entry has.
LANGUAGE_ALIASES = {"C Header": "C", "C++ Header": "C++"}
NOT_A_SOLUTION_LANGUAGE = {"Dockerfile"}


def code_languages(per_language):
    """{language: code lines} for the languages S counts, from static_axes.measure's per_language."""
    out = {}
    for name, stats in per_language.items():
        if not stats.get("counted") or not stats.get("code") or name in NOT_A_SOLUTION_LANGUAGE:
            continue
        name = LANGUAGE_ALIASES.get(name, name)
        out[name] = out.get(name, 0) + int(stats["code"])
    return out


def static_axes(checkout_dir, track, ref, cpuset=None):
    """Return {"S": int, "D": int, "source": "measured" | "stubbed", "detail": str}.

    ref is the track's reference block from the board config (S, D of the reference).
    cpuset keeps the tools container on the builder's cores (the E-cores), never on the
    P-cores a timed run may be using.
    """
    mod = _module()
    measure = getattr(mod, "measure", None) if mod else None
    if not callable(measure):
        _warn_once("harness/static_axes.py is missing; S and D are set to the reference's")
        return {"S": int(ref["S"]), "D": int(ref["D"]), "source": "stubbed",
                "detail": "S and D are not measured; set to the reference's, so they do not move K"}
    track_dir = os.path.join(checkout_dir, f"track-{track}")
    try:
        report = measure(track_dir, cpuset=cpuset)
        notes = report.get("notes") or []
        return {"S": int(report["S_lines"]), "D": int(report["D"]), "source": "measured",
                "detail": f"{len(notes)} note(s)" + (f": {notes[0][:160]}" if notes else ""),
                "langs": code_languages(report.get("per_language") or {})}
    except Exception as exc:  # a broken measurement must not stop the run; the stub keeps K computable
        log.exception("static_axes.measure failed for %s", track_dir)
        return {"S": int(ref["S"]), "D": int(ref["D"]), "source": "stubbed",
                "detail": f"static_axes failed ({type(exc).__name__}); S and D set to the reference's"}
