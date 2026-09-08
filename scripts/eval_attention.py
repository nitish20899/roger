#!/usr/bin/env python
"""Score the live "is this for me?" classifier on labeled meeting snippets.

Run from the repository root with your .env in place:  python scripts/eval_attention.py
Costs a fraction of a cent. Prints accuracy, the misses, and classifier latency.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roger.attention import Turn, make_anthropic_classifier, make_openai_classifier  # noqa: E402
from roger.config import Settings, load_env  # noqa: E402

NAME = "Roger"

CASES = [
    # (context turns, speaker, utterance, expected_addressed). ">>" marks the bot's own turns.
    ([("Alice", "Roger, which port does the bridge listen on?"), (">>", "It listens on port eight seven eight seven.")], "Alice", "And can we change that from the env file?", True),
    ([("Alice", "Roger, which port does the bridge listen on?"), (">>", "It listens on port eight seven eight seven.")], "Bob", "Why eight seven eight seven though?", True),
    ([("Alice", "Roger, which port does the bridge listen on?"), (">>", "It listens on port eight seven eight seven.")], "Bob", "Alice, did you update the firewall rules for that?", False),
    ([("Alice", "Roger, which port does the bridge listen on?"), (">>", "It listens on port eight seven eight seven.")], "Alice", "Ok cool.", False),
    ([("Alice", "Roger, which port does the bridge listen on?"), (">>", "It listens on port eight seven eight seven.")], "Carol", "Anyway, about the budget review next week.", False),
    ([("Alice", "Roger, what did we decide about the vendor?"), (">>", "We picked Attendee because Recall needs a work email.")], "Alice", "What does it cost per hour?", True),
    ([("Alice", "Roger, what did we decide about the vendor?"), (">>", "We picked Attendee because Recall needs a work email.")], "Bob", "I think that's fine, honestly.", False),
    ([("Alice", "Roger, what did we decide about the vendor?"), (">>", "We picked Attendee because Recall needs a work email.")], "Bob", "Can you post the pricing link in the chat?", True),
    ([("Alice", "Roger, what did we decide about the vendor?"), (">>", "We picked Attendee because Recall needs a work email.")], "Bob", "Carol, can you post the pricing link in the chat?", False),
    ([("Alice", "Roger, summarize the latency numbers."), (">>", "About one second from the end of a question to the first word.")], "Dan", "That's too slow, no?", True),
    ([("Alice", "Roger, summarize the latency numbers."), (">>", "About one second from the end of a question to the first word.")], "Dan", "Alice, that's too slow for us, no?", False),
    ([("Alice", "Roger, summarize the latency numbers."), (">>", "About one second from the end of a question to the first word.")], "Alice", "Yeah I agree with Dan.", False),
    ([("Alice", "Roger, summarize the latency numbers."), (">>", "About one second from the end of a question to the first word.")], "Alice", "How would you bring that under a second?", True),
    ([("Bob", "Roger, are you there?"), (">>", "Yes, I'm here.")], "Bob", "Great. So, everyone, let's start with the standup.", False),
    ([("Bob", "Roger, are you there?"), (">>", "Yes, I'm here.")], "Bob", "Tell us what changed since yesterday in the repo.", True),
    ([("Bob", "Roger, what's the project structure?"), (">>", "One folder with the design doc, a poc subfolder and a research subfolder.")], "Eve", "Where's the research folder?", True),
    ([("Bob", "Roger, what's the project structure?"), (">>", "One folder with the design doc, a poc subfolder and a research subfolder.")], "Eve", "Bob, where did you put the research folder?", False),
    ([("Bob", "Roger, what's the project structure?"), (">>", "One folder with the design doc, a poc subfolder and a research subfolder.")], "Frank", "Hmm, that doesn't sound right to me.", True),
    ([("Bob", "Roger, what's the project structure?"), (">>", "One folder with the design doc, a poc subfolder and a research subfolder.")], "Frank", "Bob, that doesn't sound right, you moved it last week.", False),
    ([("Gus", "Roger, mute for a second."), ("Gus", "So, Hal, how was the client call?"), ("Hal", "Went well, they want the demo Thursday.")], "Gus", "Can we make Thursday?", False),
    ([("Alice", "Roger, tell me about ElevenLabs."), (">>", "It handles both transcription and speech for the bot."), ("Alice", "Nice.")], "Alice", "And what about the voices, can we clone one?", True),
    ([("Alice", "Roger, tell me about ElevenLabs."), (">>", "It handles both transcription and speech for the bot.")], "Bob", "Sorry I'm late everyone, connection issues.", False),
    ([("Alice", "Roger, tell me about ElevenLabs."), (">>", "It handles both transcription and speech for the bot.")], "Bob", "Wait, say that again?", True),
    ([("Alice", "Roger, tell me about ElevenLabs."), (">>", "It handles both transcription and speech for the bot.")], "Carol", "Alice, wait, say that again?", False),
]


async def main() -> None:
    load_env()
    s = Settings.from_env()
    if s.fast_provider == "openai":
        classify = make_openai_classifier(s.openai_api_key, s.classifier_model)
    else:
        classify = make_anthropic_classifier(s.anthropic_api_key, s.classifier_model)
    print(f"classifier: {s.fast_provider} / {s.classifier_model}\n")

    async def one(case):
        ctx_raw, speaker, text, expected = case
        ctx = [Turn("bot" if who == ">>" else who, t, is_bot=(who == ">>")) for who, t in ctx_raw]
        t0 = time.time()
        res = await classify(NAME, ctx, Turn(speaker, text))
        got = bool(res["addressed"]) and res["confidence"] >= 0.6
        return case, res, got, time.time() - t0

    results = await asyncio.gather(*(one(c) for c in CASES))
    correct = 0
    lat = []
    for (ctx_raw, speaker, text, expected), res, got, dt in results:
        lat.append(dt)
        ok = got == expected
        correct += ok
        if not ok:
            print(f"MISS expected={'yes' if expected else 'no'} got={'yes' if got else 'no'} conf={res['confidence']:.2f} | {speaker}: {text}\n     reason: {res.get('reason')}")
    lat.sort()
    print(f"\naccuracy {correct}/{len(CASES)} = {correct / len(CASES):.0%}   latency p50 {lat[len(lat) // 2] * 1000:.0f} ms, max {lat[-1] * 1000:.0f} ms")


if __name__ == "__main__":
    asyncio.run(main())
