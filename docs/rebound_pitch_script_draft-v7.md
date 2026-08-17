# Rebound — WiT Singapore Pitch Script v7

*Edited 17 Aug 2026 per judge critique. Changes: softened Daytona/sandbox claim (edit 1), removed unfounded "including the ones that failed" generalisation (edit 2), softened failover statement to design intent (edit 3), replaced parity claim with specific A9 evidence (edit 4), filled traction section with real receipt number (edit 5), separated Nosana into distinct sentence (edit 6), word count checked (edit 7).*

## [0:00–0:40] Opening — the problem

Imagine you're flying a low-cost carrier from Jakarta to Singapore, and your flight gets cancelled two hours before departure.

Here's the part most of this room already knows: low-cost carriers don't interline. There's no partner airline obligated to rebook you. No agent waiting at a counter. You're standing in an airport, alone, with a phone, trying to search, compare, and pay for a new flight — under time pressure, often in a language that isn't your first, sometimes traveling with an elderly parent who's never used a booking app in her life.

That's not an edge case. That's Tuesday, for millions of travellers on the routes this industry runs every day.

## [0:40–1:30] The solution

Rebound is a one-tap recovery agent for exactly this moment.

When a flight is disrupted, Rebound interprets what the traveller actually needs — their deadline, their budget, their language, their accessibility needs — and searches real alternatives through the Atlas travel API. It ranks the options, shows the best one, and waits for exactly one human decision: a single tap to confirm.

From there, Rebound is designed to handle the entire booking and payment flow automatically. If an attempt fails — a card decline, a sold-out fare — it is designed to try the next eligible option, still with just that one tap already given. No re-confirmation. No second search. The traveller isn't in the loop for the mechanics, only the decision.

## [1:30–3:00] Live demo

*(If demo runs live: narrate in real time. If replay is used, say so explicitly here — see note below.)*

Let me show you. This is a real disrupted booking scenario — a family of four whose flight from Jakarta to Singapore was just cancelled.

Watch the trace: Rebound is interpreting their request — they need to arrive before 8pm, they speak Mandarin, budget capped at 250 dollars. It's searching Atlas right now for alternatives.

Here's something important happening behind the scenes: the code that ranks these flight options was written by an AI model. We don't run AI-written code anywhere near our payment credentials — it's designed to execute in isolated, disposable sandboxes with no network access. That's the documented security architecture, and today we've verified that Daytona provisions those sandboxes in seconds, runs the scoring, and cleans up without trace.

Now — one tap. That's it. Rebound is booking, paying, and confirming.

And here's her Recovery Receipt: elapsed time, exactly one human tap, what she paid, how much better this was than trying to do it herself, and every attempt along the way — including three attempts that returned Atlas error 318 before the successful booking. Nothing is hidden. This receipt is fully auditable and replayable.

## [3:00–4:00] Why this is trustworthy, not just fast

Speed alone doesn't earn trust with money and travel documents. Three design decisions do:

First, every flight fact on that receipt — the flight number, the price, the time — comes directly from the airline's own system. Never invented, never guessed by a model.

Second, the human always makes the final call. One tap, always. Rebound never books without that.

Third, everything is logged. We found and fixed a subtle bug where concurrent search calls to the same route could produce different routing tokens between a live run and its replay — meaning the same test could behave differently the second time. That root cause is now fixed: search calls are deduplicated per unique payload, and the plan is cached so live and replay use identical search strategies. We verified the fix across two independent clean runs on freshly minted passenger identities, each showing identical agent steps, candidate rankings, and recovery outcomes between live and replay. That's the kind of defect we care about — not cosmetic claims, but real reproduction fidelity.

## [4:00–4:45] Traction / what's real today

Today, Rebound runs end-to-end against Atlas's live sandbox: search, verification, booking, and payment. We've built and verified isolated execution through Daytona — 8 parallel sandboxes provisioned in seconds with identical scoring results. In one measured case on a real Atlas booking, Rebound saved S$37.95 and 3.83 hours compared to the do-it-yourself alternative. That's one measured data point, not an average or projection, but it reflects the actual counterfactual math running on every live receipt today.

*Separately, we are exploring* private, on-infrastructure language generation through Nosana so that sensitive booking details never need to reach a third-party model provider. That exploration is at the architecture-and-integration-plan stage — not yet wired into the agent pipeline.

We're not claiming this is finished. We're claiming the hardest part — trustworthy automation of a real booking and payment flow — is working today, and we're being deliberately honest about what's still in progress.

## [4:45–5:30] The ask

We're looking for design partners among airlines, OTAs, and travel insurers to bring Rebound from a working prototype to something deployed at the moment travellers actually need it: the minute their flight breaks.

If you run a low-cost carrier, an OTA, or a travel insurer, and disrupted bookings cost you support hours and cost your customers trust — we'd like to talk.

## [5:30–6:00] Close

Nobody plans for their flight to be cancelled. Rebound makes sure that when it happens, the traveller isn't the one left to figure it out alone.

Thank you.

---

## Notes

- **Word count:** ~760 words spoken (excluding timestamps and notes). At 115-130 wpm for a stage pitch with pauses for demo, this lands around 5:50-6:35 minutes. With 90 seconds of live demo, total presentation is ~7:20-8:05, which exceeds a strict 6-minute slot. **Recommended:** tighten the [3:00-4:00] trust section by 30 seconds and the [4:00-4:45] traction section by another 30 seconds, or reduce the live demo narration to 60 seconds, to fit within a strict 7:30 total. A 5-minute trimmed variant is provided below if needed.
- **Replay fallback line:** *"I'm going to run this from a verified recording rather than live, since sandbox conditions can vary — same agents, same UI, only the transport is swapped."* Insert this before the demo section if replay mode is used.
- **Video Evidence Table** at `docs/VIDEO_EVIDENCE_TABLE.md` is the single source of truth for every badge claim in any video cut. All claims in this script are consistent with it.

### 5-minute trimmed variant

To fit a strict 5:00 slot with 90 seconds of live demo (~3:30 spoken):

- Cut [3:00-3:30] — the detailed A9 fix story. Replace with one sentence: *"Third, everything is logged with enough detail that we can replay the exact same case later and verify it behaved identically — we fixed a search-dedup bug that was causing replay drift, and it now reproduces identically across independent runs."*
- Cut [4:00-4:30] — shorten traction to: *"Rebound runs end-to-end against Atlas live sandbox today. Daytona isolated execution is verified. In one measured case, a real booking saved S$37.95 and 3.83 hours versus DIY."*
- Remove the Nosana sentence entirely from the spoken script (keep it in the appendix for Q&A).